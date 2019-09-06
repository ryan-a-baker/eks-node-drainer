"""
This module is intented to be run as a Lambda which will be used to drain
pods from a node in an EKS clusters in the event of an auto-scaling event or spot instance
termination.
"""

import sys
import time
import boto3
import yaml

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from botocore.exceptions import ClientError

def generate_kube_config(region, cluster_name):
    """
    Creates a kubeconfig file in /tmp since Lambda's have read only filesystems.

    In the future, we should build a config object instead of relying on
    a file.

    Parameters:
    region (string): The region that the lambda was invoked from
    cluster_name (string):  The name of the K8S cluster that the drain action
                            should be performed on.
    """

    config_file = "/tmp/kubeconfig"

    # set up the client
    eks_client = boto3.Session(region_name=region)
    eks = eks_client.client("eks")

    # get cluster details
    cluster = eks.describe_cluster(name=cluster_name)
    cluster_cert = cluster["cluster"]["certificateAuthority"]["data"]
    cluster_ep = cluster["cluster"]["endpoint"]

    # build the cluster config hash
    cluster_config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": {
                    "server": str(cluster_ep),
                    "certificate-authority-data": str(cluster_cert)
                },
                "name": str(cluster_name)
            }
        ],
        "contexts": [
            {
                "context": {
                    "cluster": str(cluster_name),
                    "user": str(cluster_name)
                },
                "name": str(cluster_name)
            }
        ],
        "current-context": str(cluster_name),
        "preferences": {},
        "users": [
            {
                "name": str(cluster_name),
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1alpha1",
                        "command": "./aws-iam-authenticator",
                        "args": [
                            "token", "-i", str(cluster_name)
                        ]
                    }
                }
            }
        ]
    }

    # Write out the yaml configuration file to /tmp since lambda is read-only
    config_text = yaml.dump(cluster_config, default_flow_style=False)
    open(config_file, "w").write(config_text)

def get_hostname(event):
    """
    Retrieves the hostname for a given instance_id to be used for future K8S
    calls.

    If we fail to retrieve a hostname, we should exit the lambda with a failure
    code.  Eventually, the lifecycle hook will timeout, and continue on.

    In the future, we should "ABORT" the lifecycle hook.  I was unable to get
    this working (it just kept moving forward) so leaving it as is for now.

    Parameters:
    event (object): The event that the lambda received from the CloudWatch hook
    """

    ec2 = boto3.client('ec2')

    instance_id = event['detail']['EC2InstanceId']

    # if we fail to get the private DNS, go ahead and fail since we can't do much else
    try:
        private_dns = ec2.describe_instances(InstanceIds=[instance_id]) \
                            ['Reservations'][0]['Instances'][0]['PrivateDnsName']
    except ClientError as e:
        print("Exception when converting %s to private DNS: %s" % (instance_id, e))
        sys.exit(1)

    return private_dns

def cordon_node(api_instance, node):
    """
    Cordons a specified node so that pods can no longer be scheduled to it.

    Parameters:
    api_instance (object): The K8S API object to use
    node (string): The hostname of server to cordon

    """

    print("Cordoning Node " + node)

    # Doesn't look like there is a way to build the body object, so build it
    # manually
    body = {
        "spec": {
            "unschedulable": True
        }
    }

    # Appears that patching a node is the only way to do this
    try:
        api_instance.patch_node(node, body)
    except ApiException as e:
        print("Exception when cordoning %s: %s\n" % (node, e))

def evict_pod(api_instance, name, namespace):
    """
    Evict a given pod so that it will be rescheduled on a schedulable node.

    We do catch errors and log them here, however, we don't halt the whole
    process because the node needs to go down regardless.  This makes our
    evicting a "best effort".

    Parameters:
    api_instance (object): The K8S API object to use
    name (string): The name of the pod to evict
    namespace (string): The namespace the pod to evict is in
    """

    print("Evicting " + name + " in namespace " + namespace + "!")

    delete_options = client.V1DeleteOptions()
    # After checking pod status for 12 minute, we'll assume the any remaining
    # pods won't evict, and tell the lifecycle hook to move on.  Let's do 12 minutes
    # + 30 seconds to ungracefully terminate any remaining pods, yet still let us
    # list any pods that will be ungracefully terminated
    delete_options.grace_period_seconds = 750

    metadata = client.V1ObjectMeta(name=name, namespace=namespace)
    body = client.V1beta1Eviction(metadata=metadata,
                                  api_version="policy/v1beta1",
                                  kind="Eviction",
                                  delete_options=delete_options)

    try:
        api_instance.create_namespaced_pod_eviction(name=name,
                                                    namespace=namespace,
                                                    body=body)
    except ApiException as e:
        print("Exception when evicting %s: %s\n" % (name, e))

def continue_lifecycle(life_cycle_hook, auto_scaling_group, instance_id):
    """
    Tell the lifecycle hook that we have completed our action and continue on
    with the termination of the node.

    Parameters:
    event (object): The event that the lamba object recieved
    """

    asg = boto3.client('autoscaling')

    # We'll give this a shot, but if it fails, it will eventualy timeout and
    # continue, so don't halt the whole process
    try:
        asg.complete_lifecycle_action(
            LifecycleHookName=life_cycle_hook,
            AutoScalingGroupName=auto_scaling_group,
            LifecycleActionResult='CONTINUE',
            InstanceId=instance_id)
    except ClientError as e:
        print("Exception in advancing lifecycle event %s: %s\n" % (life_cycle_hook, e))

def get_evictable_pods(api_instance, node_name):
    """
    Get a list of all evictable pods currently on the supplied node.
    This would not include pods which are controlled by a daemonset.

    Parameters:
    api_instance (object): The K8S API object to use
    node_name (string): The name of the node to retrieve the pod list

    Returns:
    pods_to_evict (list): List of pods that should be evicted from the node

    """
    field_selector = 'spec.nodeName=' + node_name

    pod_list = api_instance.list_pod_for_all_namespaces(watch=False, field_selector=field_selector)
    pods_to_evict = []

    for pod in pod_list.items:
        if pod.metadata.owner_references[0].kind != 'DaemonSet':
            pods_to_evict.append(pod)

    return pods_to_evict

def lambda_handler(event, context):
    """
    Coordinate the draining of the node specified by the scaling event.

    This runs through the following flow:

    1.  Convert the instance ID to a hostname to be used by K8S
    2.  Generate a kubeconfig file that can be loaded for K8S API calls
    3.  Cordon the node
    4.  Loop through every pod on the node, evicting individually
    5.  Wait for all pods to evict
    6.  Tell the lifecycle hook to continue on

    Unfortunately, K8S does not have a "drain" API, so we have to do it client
    side.  There is an effort to move this to server side, so in the future
    we can move to that API call.

    Parameters:
    event (object): The event that the lamba object recieved
    context (object): Provides methods and properties that provide information
                      about the invocation, function, and execution environment
    """

    # Convert the Instance ID to Hostname
    node_name = get_hostname(event)

    # Generate a kube config file from the information in the event
    generate_kube_config(region=event['region'],
                         cluster_name=event['detail']['NotificationMetadata'])

    # Load the config file and init the client
    config.load_kube_config("/tmp/kubeconfig")
    api_instance = client.CoreV1Api()

    # Cordon the node to be drained
    print("Recieved a request to evict node " + node_name)
    cordon_node(api_instance=api_instance, node=node_name)

    # Get a list of all the pods that are evictable (excludes pods managed by daemonset)
    pods = get_evictable_pods(api_instance, node_name)

    # loop through all pods and try to evict them from the node
    for pod in pods:
        evict_pod(api_instance=api_instance,
                  name=pod.metadata.name,
                  namespace=pod.metadata.namespace)

    remaining_pods = []

    # Max Lambda time is 15 minutes, let's wait for all pods to be evicted
    # for 12 minutes to give ourselves a buffer
    endtime = time.time() + 60 * 3

    while time.time() < endtime:
        remaining_pods = get_evictable_pods(api_instance=api_instance, node_name=node_name)
        if not remaining_pods:
            print("All pods have been evicted.  Safe to proceed with node termination")
            break
        print("Waiting for " + str(len(remaining_pods)) + " pods to evict!")
        time.sleep(5)

    if remaining_pods:
        print("The following pods did not drain successfully:")
        for pod in remaining_pods:
            print(pod.metadata.namespace + "/" + pod.metadata.name)
        # After we issue the evict to the pods, we give it 12 minutes to do so.  After which,
        # the evict process will ungracefully terminate pods.  Let's give it 30 seconds to
        # ungracefully evict, then continue on.
        #
        # This may not matter since the node is being terminated, but might as well
        # let evict do it's thing
        time.sleep(30)

    # Tell the lifecycle hook to continue on
    continue_lifecycle(life_cycle_hook=event['detail']['LifecycleHookName'],
                       auto_scaling_group=event['detail']['AutoScalingGroupName'],
                       instance_id=event['detail']['EC2InstanceId'])

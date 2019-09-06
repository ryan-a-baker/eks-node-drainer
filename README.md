# eks-node-drainer

A Framework to drain pods from nodes before termination for EKS.  A full blog post write-up of the service and all it's components can be found [here](https://ryanbaker.io/2019-09-04-eks-node-drainer/)

# Deploying the Project

Deploying the service is a simple 3 step process.

1. Create the ASG Lifecycle Hook
2. Deploy the CloudFormation which creates the CloudWatch Event Rule, Lambda, and the needed IAM Roles.
3. Apply the K8S roles to allow the Lambda to authenticate with K8S

## Create the ASG Lifecycle Hook

The auto-scaling group for the EKS cluster is deployed as part of the [cluster configuration](https://amazon-eks.s3-us-west-2.amazonaws.com/cloudformation/2019-01-
09/amazon-eks-nodegroup.yaml) that Amazon provides in their [quick-start guide](https://s3.amazonaws.com/aws-quickstart/quickstart-amazon-eks/doc/amazon-eks-architecture.pdf).  Because of this, this is the only part of this deployment that you will have to manually do.  Ideally, it would be best to update the Cloudformation you used to launch your cluster, but given that Amazon has released many versions of this template, it would be difficult to document every permutation, but doing it manually will work fine.  Just make sure if you run an update via CloudFormation after this is added that you ensure the lifecycle hook persists as it could be removed since it is added out-of-band.

It's simple to add the lifecycle hook, just navigate to the AWS EC2 Dashboard->Auto Scaling Groups and locate your clusters auto scaling group.  It will be named the same as your cluster + "-cluster-NodeGroup-<random string>" appended to the end.  Once selected, navigate to the "Lifecycle Hook" tab and click "Create Lifecycle Hook" button.

![Create Lifecycle Hook](https://github.com/ryan-a-baker/ryanbakerio/blob/master/img/lifecyclehookcreate.png?raw=true){: .center-block :}

Fill out the following information:

| Field | Value |
| ----- | ----- |
| Lifecycle Hook Name | Arbitrary.  Name it whatever you like that makes sense |
| Lifecycle Transition | We only need to take action when a node is terminated, so choose "Instance Terminate" |
| Heartbeat Timeout | 300 is what I found works the best for our workloads.  However, see the section below titled timing for further explanation |
| Default Result | This will be what happens when the timeout is reached.  We chose abandon to kill of the lifecycle hook.  Choosing continue would just allow the terminate of the instance to continue |
| Notification Metadata | Put the name of your cluster here.  This is important because it will be passed to the Lambda, which is used to build the K8S context within the Lambda |

Once it's created, it should look something like this:

![Lifecycle Hook Created](https://github.com/ryan-a-baker/ryanbakerio/blob/master/img/lifecyclehookcreated.png?raw=true){: .center-block :}

Take a deep breath, that's the last manual thing you'll have to do.  The rest is all defined by running the CloudFormation template.

##  Deploy the CloudFormation

Assuming you have the AWS CLI setup, you can run the CloudFormation template with the following:

```
aws cloudformation update-stack \
    --template-body file://cloudformation.yaml \
    --stack-name eks-node-drainer \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters ParameterKey=ASGToWatch,ParameterValue=\"eks-us-west-2-testing-cluster-NodeGroup-15RUYBMP2JKKA,eks-us-west-2-non-prod-cluster-NodeGroup-1LY177EYAP560,eks-us-west-2-prod-cluster-NodeGroup-50N357EH0MVF\" \
    ParameterKey=LambdaVPCSubnets,ParameterValue=\"subnet-5f377226,subnet-9493f4df\" \
    ParameterKey=LambdaVPCID,ParameterValue="vpc-b10c51da\n"

aws transcribe start-transcription-job \
     --region region \
     --cli-input-json file://test-start-command.json
```

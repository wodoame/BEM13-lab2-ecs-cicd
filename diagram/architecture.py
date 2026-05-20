"""
BEM13 Lab2 - ECS CI/CD Architecture Diagram
Run: pip install diagrams && python architecture.py
Output: architecture.png in the current directory
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import ECS, Fargate
from diagrams.aws.network import ALB, InternetGateway, NATGateway, VPCEndpoint
from diagrams.aws.storage import S3
from diagrams.aws.devtools import Codepipeline, Codedeploy
from diagrams.aws.integration import Eventbridge
from diagrams.aws.security import IAMRole
from diagrams.aws.management import Cloudwatch
from diagrams.aws.general import User
from diagrams.onprem.vcs import Github
from diagrams.aws.storage import ECR

graph_attr = {
    "fontsize": "13",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "ortho",
}

cluster_attr = {"fontsize": "11"}

with Diagram(
    "BEM13 Lab2 - ECS CI/CD Architecture",
    filename="diagram/architecture",
    outformat="png",
    graph_attr=graph_attr,
    direction="LR",
    show=False,
):
    developer = Github("Developer\n(push to main)")
    internet_user = User("End User")

    with Cluster("AWS Cloud  ·  us-east-1"):

        ecr = ECR("ECR\nbem13-lab2-app")
        eb = Eventbridge("EventBridge\nECR Push Rule")
        pipeline = Codepipeline("CodePipeline\nbem13-lab2-pipeline")
        codedeploy = Codedeploy("CodeDeploy\nBlue/Green")
        s3 = S3("S3 Artifact\nBucket")
        logs = Cloudwatch("CloudWatch\nLogs")
        oidc_role = IAMRole("GitHub OIDC\nIAM Role")

        with Cluster("VPC  ·  10.0.0.0/16", graph_attr=cluster_attr):

            with Cluster("Public Subnets (us-east-1a/b)", graph_attr=cluster_attr):
                igw = InternetGateway("Internet\nGateway")
                alb = ALB("ALB\nbem13-lab2-alb\nport 80 / 8080")
                nat_a = NATGateway("NAT GW\nus-east-1a")
                nat_b = NATGateway("NAT GW\nus-east-1b")

            with Cluster("Private Subnets (us-east-1a/b)", graph_attr=cluster_attr):
                with Cluster("ECS Cluster  ·  bem13-lab2-cluster"):
                    task_blue = Fargate("Task (Blue)\nport 8080")
                    task_green = Fargate("Task (Green)\nport 8080")

                vpce = VPCEndpoint("VPC Endpoints\necr.api · ecr.dkr\nlogs · s3 · ecs")

    # CI flow: developer pushes → GitHub Actions → OIDC → ECR
    developer >> Edge(label="git push") >> oidc_role
    oidc_role >> Edge(label="assume role (OIDC)") >> ecr

    # ECR push triggers pipeline
    ecr >> Edge(label="image push event") >> eb
    eb >> Edge(label="StartPipelineExecution") >> pipeline
    s3 >> Edge(label="appspec.yaml\ntaskdef.json") >> pipeline
    pipeline >> codedeploy

    # CodeDeploy blue/green to ALB
    codedeploy >> Edge(label="deploy new\ntask set") >> alb
    alb >> Edge(label="prod :80\n→ blue TG") >> task_blue
    alb >> Edge(label="test :8080\n→ green TG") >> task_green

    # User traffic
    internet_user >> Edge(label="HTTP :80") >> igw >> alb

    # Private subnet outbound via NAT
    task_blue >> nat_a
    task_green >> nat_b

    # Image pull via VPC endpoints
    task_blue >> Edge(label="pull image") >> vpce >> ecr
    task_green >> Edge(label="pull image") >> vpce >> ecr

    # Logs
    task_blue >> logs
    task_green >> logs

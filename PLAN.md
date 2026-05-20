# ECS CI/CD Implementation Plan

## Overview

Deploy a containerized Java web application on Amazon ECS Fargate with full CI/CD automation.

**Two-repo strategy:**
- **This repo (`BEM13-lab2-ecs-cicd`):** CloudFormation templates, GitSync config, architecture diagram
- **New repo (`BEM13-lab2-ecs-app`):** Java Spring Boot app, Dockerfile, GitHub Actions CI workflow, `appspec.yaml`, `taskdef.json`

**Region:** `us-east-1`

---

## Repository Structure (This Repo)

```
BEM13-lab2-ecs-cicd/
├── deployment-file.yaml          # CloudFormation GitSync config
├── root-stack.yaml               # Root/parent nested stack
├── templates/
│   ├── ecr-stack.yaml
│   ├── vpc-stack.yaml
│   ├── security-groups-stack.yaml
│   ├── vpc-endpoints-stack.yaml
│   ├── alb-stack.yaml
│   ├── iam-stack.yaml
│   ├── ecs-cluster-stack.yaml
│   ├── codedeploy-stack.yaml
│   ├── ecs-service-stack.yaml
│   ├── autoscaling-stack.yaml
│   └── pipeline-stack.yaml
└── diagram/
    └── architecture.py
```

---

## Phase 1: CloudFormation Infrastructure

### VPC Design

| Subnet | CIDR | AZ | Type |
|---|---|---|---|
| `bem13-public-1a` | `10.0.1.0/24` | us-east-1a | Public |
| `bem13-public-1b` | `10.0.2.0/24` | us-east-1b | Public |
| `bem13-private-1a` | `10.0.11.0/24` | us-east-1a | Private (ECS) |
| `bem13-private-1b` | `10.0.12.0/24` | us-east-1b | Private (ECS) |

- 1 Internet Gateway
- 2 NAT Gateways (one per AZ for HA), each with an Elastic IP
- VPC Endpoints (Interface): `ecr.api`, `ecr.dkr`, `logs`, `ecs`, `ecs-agent`, `ecs-telemetry`
- VPC Endpoint (Gateway): `s3` (ECR uses S3 for layer data)

### Stack Dependency Order

```
ecr-stack
  └─ iam-stack (needs ECR ARN)
vpc-stack
  └─ security-groups-stack
       ├─ vpc-endpoints-stack
       └─ alb-stack
            └─ codedeploy-stack
                 └─ ecs-service-stack
ecs-cluster-stack (depends on vpc-stack)
autoscaling-stack (depends on ecs-service-stack)
pipeline-stack (depends on ecr-stack + codedeploy-stack)
```

### Stack Specifications

#### `ecr-stack.yaml`
- `AWS::ECR::Repository`: name `bem13-lab2-app`, scan on push, lifecycle policy (keep last 10 images)

#### `vpc-stack.yaml`
- VPC `10.0.0.0/16` with DNS hostnames + support enabled
- 4 subnets, 1 IGW, 2 NAT GWs, 2 EIPs, route tables + associations
- Public route tables: `0.0.0.0/0` → IGW
- Private route tables (per AZ): `0.0.0.0/0` → NAT GW in same AZ

#### `security-groups-stack.yaml`
- **`bem13-alb-sg`**: inbound TCP 80 + 8080 from `0.0.0.0/0`; outbound TCP 8080 to ECS SG
- **`bem13-ecs-sg`**: inbound TCP 8080 from ALB SG only; outbound TCP 443 to VPC CIDR
- **`bem13-vpce-sg`**: inbound TCP 443 from `10.0.11.0/24` + `10.0.12.0/24` only

#### `vpc-endpoints-stack.yaml`
- Interface endpoints: `PrivateDnsEnabled: true`, private subnets, `bem13-vpce-sg`
- S3 Gateway endpoint: `RouteTableIds` set to both private route tables

#### `alb-stack.yaml`
- Internet-facing ALB in public subnets
- **Blue TG** (`bem13-blue-tg`): port 8080, target type `ip`, health check `GET /health`
- **Green TG** (`bem13-green-tg`): identical settings
- Production listener: port 80 → blue TG
- Test listener: port 8080 → green TG (used by CodeDeploy during deployment)

#### `iam-stack.yaml`
- **`bem13-ecs-task-execution-role`**: trust `ecs-tasks.amazonaws.com`, `AmazonECSTaskExecutionRolePolicy` + ECR pull + CloudWatch Logs write
- **`bem13-ecs-task-role`**: trust `ecs-tasks.amazonaws.com`, minimal permissions
- **`bem13-codedeploy-role`**: trust `codedeploy.amazonaws.com`, `AWSCodeDeployRoleForECS`
- **`bem13-codepipeline-role`**: trust `codepipeline.amazonaws.com`, CodeDeploy + S3 + ECS + IAM PassRole
- **`bem13-github-actions-oidc-role`**: OIDC federated trust for `token.actions.githubusercontent.com`, scoped to `repo:wodoame/BEM13-lab2-ecs-app:ref:refs/heads/main`, ECR push permissions
- **`bem13-eventbridge-pipeline-role`**: trust `events.amazonaws.com`, `codepipeline:StartPipelineExecution`
- **`AWS::IAM::OIDCProvider`**: `https://token.actions.githubusercontent.com` with GitHub thumbprints (`DeletionPolicy: Retain`)

#### `ecs-cluster-stack.yaml`
- `AWS::ECS::Cluster`: `bem13-lab2-cluster`, Container Insights enabled, FARGATE + FARGATE_SPOT capacity providers
- `AWS::Logs::LogGroup`: `/ecs/bem13-lab2-app`, 30-day retention

#### `codedeploy-stack.yaml`
- `AWS::CodeDeploy::Application`: `bem13-lab2-app`, compute platform `ECS`
- `AWS::CodeDeploy::DeploymentGroup`: `bem13-lab2-dg`
  - Blue/green with traffic control
  - `CodeDeployDefault.ECSAllAtOnce`
  - Terminate blue after 5 min
  - ALB blue/green target group pair (prod listener port 80, test listener port 8080)

#### `ecs-service-stack.yaml`
- `AWS::ECS::TaskDefinition`: family `bem13-lab2-app`, 256 CPU / 512 MB, `awsvpc`, Fargate
  - Container port 8080, CloudWatch Logs driver, health check `curl http://localhost:8080/health`
- `AWS::ECS::Service`: `bem13-lab2-svc`, desired 1, `AssignPublicIp: DISABLED`, private subnets
  - `DeploymentController: CODE_DEPLOY`, health check grace period 60s

#### `autoscaling-stack.yaml`
- Scalable target: min **1**, desired **1**, max **4** on `ecs:service:DesiredCount`
- Scale-out: +1 task when `CPUUtilization >= 70%` (2 consecutive 60s periods)
- Scale-in: −1 task when `CPUUtilization <= 30%` (5 consecutive 60s periods, longer cooldown to prevent flapping)

#### `pipeline-stack.yaml`
- S3 artifact bucket: versioning enabled, SSE-S3, all public access blocked, 30-day lifecycle
- EventBridge rule: ECR image push on `bem13-lab2-app` → triggers CodePipeline
- CodePipeline (2-source):
  - Source 1: ECR `ImageTag: latest` → `ImageArtifact` (produces `imageDetail.json`)
  - Source 2: S3 `deploy/deploy-bundle.zip` → `DeployArtifact` (contains `appspec.yaml` + `taskdef.json`)
  - Deploy: `CodeDeployToECS` with `Image1ContainerName: IMAGE1_NAME` substitution

### GitSync Config (`deployment-file.yaml`)

```yaml
template-file-path: /root-stack.yaml
parameters:
  Environment: production
tags:
  Project: bem13-lab2-ecs-cicd
```

### Tagging Strategy (all resources)

```
Project:     bem13-lab2-ecs-cicd
Owner:       <your-name>
Environment: production
ManagedBy:   CloudFormation
```

---

## Phase 2: Application Code (New Repo)

### App Repo Structure

```
BEM13-lab2-ecs-app/
├── src/main/java/com/bem13/lab2/
│   ├── Lab2Application.java
│   └── HomeController.java
├── src/main/resources/templates/index.html
├── pom.xml
├── Dockerfile
├── appspec.yaml
├── taskdef.json
└── .github/workflows/ci.yml
```

### Application

- Spring Boot 3.x, Maven, embedded Tomcat on port 8080
- `GET /` → HTML page with full name + "ECS CI/CD Lab"
- `GET /health` → `200 OK` plain text (ALB health check)

### Dockerfile

```dockerfile
FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
COPY target/bem13-lab2-app-1.0.0.jar app.jar
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD wget -qO- http://localhost:8080/health || exit 1
ENTRYPOINT ["java", "-jar", "app.jar"]
```

### Image Tagging Strategy

- Short commit SHA (`${GITHUB_SHA::7}`) — unique and immutable per release
- `latest` — mutable floating pointer for pipeline source

### GitHub Actions CI Workflow

Trigger: `push` to `main`  
Required permissions: `id-token: write`, `contents: read`

Steps:
1. Checkout → JDK 21 (Temurin) → `mvn -B package -DskipTests`
2. `aws-actions/configure-aws-credentials@v4` — assumes `bem13-github-actions-oidc-role` via OIDC
3. `aws-actions/amazon-ecr-login@v2`
4. Docker build with SHA tag + `latest` tag
5. Docker push both tags
6. Zip `appspec.yaml` + `taskdef.json` → upload to `s3://bem13-lab2-pipeline-artifacts/deploy/deploy-bundle.zip`

Only secret needed: `AWS_ACCOUNT_ID` (GitHub repo secret)

### CodeDeploy Files

**`appspec.yaml`:**
```yaml
version: 0.0
Resources:
  - TargetService:
      Type: AWS::ECS::Service
      Properties:
        TaskDefinition: <TASK_DEFINITION>
        LoadBalancerInfo:
          ContainerName: bem13-lab2-app
          ContainerPort: 8080
        PlatformVersion: LATEST
```

**`taskdef.json`:** Full task definition JSON with `"image": "<IMAGE1_NAME>"` — replaced by CodeDeploy at deploy time using `imageDetail.json` from ECR source artifact.

---

## Phase 3: Architecture Diagram

File: `diagram/architecture.py` using Python `diagrams` library.

Components: GitHub → ECR → EventBridge → CodePipeline → CodeDeploy → ALB → ECS (blue/green tasks) inside multi-AZ VPC with VPC endpoints.

---

## Phase 4: Manual Bootstrap Steps (one-time)

1. Create GitHub CodeConnections connection in AWS Console (requires OAuth approval) — note the connection ARN
2. Deploy `iam-stack` first (or create OIDC provider manually) — needed before app repo CI can run
3. Create root CloudFormation stack with GitSync pointing to this repo's `main` branch and `deployment-file.yaml`
4. After all infra stacks deploy: create app repo, push code, CI runs, pipeline triggers automatically

---

## All Resource Names

| Resource | Name |
|---|---|
| ECR Repository | `bem13-lab2-app` |
| VPC | `bem13-lab2-vpc` (`10.0.0.0/16`) |
| ALB | `bem13-lab2-alb` |
| ECS Cluster | `bem13-lab2-cluster` |
| ECS Service | `bem13-lab2-svc` |
| Task Definition Family | `bem13-lab2-app` |
| Blue Target Group | `bem13-blue-tg` |
| Green Target Group | `bem13-green-tg` |
| CodeDeploy App | `bem13-lab2-app` |
| CodeDeploy Deployment Group | `bem13-lab2-dg` |
| CodePipeline | `bem13-lab2-pipeline` |
| Artifact Bucket | `bem13-lab2-pipeline-artifacts` |
| EventBridge Rule | `bem13-ecr-image-push` |
| Log Group | `/ecs/bem13-lab2-app` |
| GitHub OIDC Role | `bem13-github-actions-oidc-role` |
| Task Execution Role | `bem13-ecs-task-execution-role` |
| Task Role | `bem13-ecs-task-role` |
| CodeDeploy Role | `bem13-codedeploy-role` |
| CodePipeline Role | `bem13-codepipeline-role` |
| EventBridge Role | `bem13-eventbridge-pipeline-role` |

---

## Known Pitfalls

| Pitfall | Solution |
|---|---|
| OIDC provider already exists in account | Add `DeletionPolicy: Retain` to `AWS::IAM::OIDCProvider` |
| ECS service with `CODE_DEPLOY` controller — CloudFormation can't update task definition | CFN owns service config; CodeDeploy owns deployments |
| S3 Gateway endpoint not routing ECR layer pulls | Explicitly set `RouteTableIds` on the endpoint resource |
| CodePipeline S3 source requires bucket versioning | Include `VersioningConfiguration: Status: Enabled` |
| Interface endpoints must resolve via private DNS | Set `PrivateDnsEnabled: true` on all Interface endpoints |
| CodeDeploy deployment group references ECS service by name | ECS service stack must deploy before CodeDeploy stack |

---

## Verification Checklist

- [ ] All CloudFormation stacks deploy without errors
- [ ] ECS tasks appear healthy in ALB target group
- [ ] `curl http://<ALB-DNS>/` returns HTML with full name and lab name
- [ ] `curl http://<ALB-DNS>/health` returns `200 OK`
- [ ] Push to app repo triggers GitHub Actions; image appears in ECR
- [ ] EventBridge rule fires → CodePipeline execution starts automatically
- [ ] CodeDeploy completes blue/green deployment; traffic shifts; old task set terminates
- [ ] CloudWatch Logs group `/ecs/bem13-lab2-app` shows container logs
- [ ] Auto Scaling target shows min 1 / max 4 in AWS Console
- [ ] Architecture diagram renders as PNG

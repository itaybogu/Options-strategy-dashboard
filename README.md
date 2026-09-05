## In a Nutshell

A small **DevOps + quantitative project** built to learn and apply
common DevOps principles such as **CI/CD, Terraform, AWS, and Kubernetes**.

The dashboard currently implements four options strategies:

### 1. Vertical Spreads
Based on the logic presented in [this video](https://www.youtube.com/watch?v=6I5a3QX4y0).

### 2. Calendar Spreads
Based on the logic presented in [this video](https://www.youtube.com/watch?v=6ao3uXE5KhU&t=1914s).

### 3. Pre-Earnings Short Straddles
Based on the logic presented in [this video](https://www.youtube.com/watch?v=oW6MHjzxHpU&t=476s).

### 4. Cash-Secured Puts
Based on FCF, following the methodology presented in
[this Goldman Sachs paper](https://www.docdroid.net/iMJWbcb/goldman-sachs-the-art-of-put-selling-pdf).


# Infrastructure

Terraform + Kubernetes setup for the Options Strategy Dashboard on AWS EKS.

Terraform builds the VPC and the cluster itself (plus Traefik for ingress).
The k8s manifests deploy the app on top of that. Day-to-day app updates go
through the CI/CD pipeline (build → test → push → rolling restart).

## Layout

```
terraform/
  providers.tf   AWS + Terraform version constraints
  variables.tf   region, cluster name, VPC CIDR, environment
  vpc.tf         VPC, public/private subnets, NAT gateway
  eks.tf         EKS cluster, node group, add-ons
  helm.tf        Traefik install via the Helm provider
  outputs.tf     cluster_endpoint, cluster_name, vpc_id

k8s/
  deployment.yaml   2 replicas of the dashboard
  service.yaml      ClusterIP, 80 -> 8000
  ingress.yaml      Traefik, catches everything on /
  hpa.yaml          scales 2-4 replicas at 50% CPU
```

## What's in here

VPC across 2 AZs, private subnets for the cluster/nodes, public subnets for
a single NAT gateway (one NAT, not one per AZ — cheaper, and fine for what
this needs).

EKS 1.36 with the public endpoint on, one managed node group running
`t3.small`s (min 1, max 2, desired 2), and the standard add-ons —
`vpc-cni`, `coredns`, `kube-proxy`, and `metrics-server`. (metrics-server is essential for the HPA to work)

Traefik goes in through Terraform's Helm provider rather than a separate
`helm install`, so it lives in the same state as everything else.

On the k8s side: the deployment runs whatever's in `k8s/deployment.yaml`'s
image field on 2 pods (256Mi/250m requested, capped at 512Mi/500m), a
ClusterIP service fronts it, Traefik ingress routes all paths to that
service, and the HPA scales up to 4 replicas past 50% average CPU.

## Prereqs

Terraform >= 1.5.0, AWS CLI configured, kubectl. Nothing exotic on the AWS
side — 2 AZs, 1 NAT gateway, a couple `t3.small`s.

## GitHub Actions secrets

The pipeline (`.github/workflows/ci-cd.yml`) needs these set under repo
Settings > Secrets and variables > Actions:

- `DOCKER_USERNAME` / `DOCKER_PASSWORD` — Docker Hub login, also used as the
  push target (`$DOCKER_USERNAME/options-scanner:latest`)
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — used to run
  `aws eks update-kubeconfig` and restart the deployment. Whatever IAM
  identity these belong to needs an EKS access entry on the cluster
  (`enable_cluster_creator_admin_permissions` only covers whoever ran
  `terraform apply`) — otherwise the rollout restart step fails with an
  auth error even though the credentials themselves are valid.

One thing worth knowing: `k8s/deployment.yaml`'s image field defaults to
`itayb5/options-scanner:latest` and isn't read by the pipeline at all —
there's a comment right above it in the file if you're deploying your own
image (see Deploying below). The pipeline instead runs `kubectl set image`
with the secret on every deploy, which sets the live deployment's image
directly regardless of what's in the file.

Without these secrets the `build-and-deploy` job fails right at login/auth —
`test` still runs fine on its own since it doesn't touch Docker Hub or AWS.

## Deploying

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Then point kubectl at the cluster:

```bash
aws eks update-kubeconfig --region us-east-1 --name options-scanner-cluster
```

If you're deploying your own image rather than `itayb5/options-scanner`,
change the `image:` line in `k8s/deployment.yaml` first — there's a comment
right above it. Then apply the manifests:

```bash
kubectl apply -f k8s/
```

Traefik takes a couple minutes to get its load balancer up after `apply`
finishes — check `kubectl get svc -n kube-system` for the external hostname.
Ingress doesn't filter by host, so hitting that hostname directly works too.

## Outputs

- `cluster_endpoint` — EKS API server endpoint
- `cluster_name` — defaults to `options-scanner-cluster`
- `vpc_id`

## Variables

All have defaults, override via `terraform.tfvars` or `-var` if needed:

- `aws_region` — `us-east-1`
- `cluster_name` — `options-scanner-cluster`
- `vpc_cidr` — `10.0.0.0/16`
- `environment` — `prod`

## Tearing down

```bash
kubectl delete -f k8s/
cd terraform
terraform destroy
```

Delete the k8s resources first — otherwise Traefik's load balancer can end
up orphaned in AWS after the cluster's gone.
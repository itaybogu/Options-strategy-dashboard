## In a Nutshell

A small **DevOps + quantitative project** built to learn and apply
common DevOps principles such as **CI/CD, Terraform, AWS, Azure and Kubernetes**.

The dashboard currently implements four options strategies:

### 1. Vertical Spreads
Based on the logic presented in [this video](https://www.youtube.com/watch?v=6I5a3QQX4y0&t=909s).

### 2. Calendar Spreads
Based on the logic presented in [this video](https://www.youtube.com/watch?v=6ao3uXE5KhU&t=1914s).

### 3. Pre-Earnings Short Straddles
Based on the logic presented in [this video](https://www.youtube.com/watch?v=oW6MHjzxHpU&t=476s).

### 4. Cash-Secured Puts
Based on FCF, following the methodology presented in
[this Goldman Sachs paper](https://optionsoffice.ru/wp-content/uploads/2016/03/Goldman-Sachs_The-art-of-put-selling.pdf).


# Infrastructure

Terraform + Kubernetes setup for the Options Strategy Dashboard — deployable
to either **AWS EKS** or **Azure AKS**. Which cloud a given run actually
targets is a single switch (see "Choosing a cloud" below), not two separate
projects to maintain by hand.

Terraform builds the network and the cluster (plus Traefik for ingress) on
whichever cloud you pick. The k8s manifests are cloud-agnostic — same
`k8s/` folder deploys on top of either. Day-to-day app updates go through
the CI/CD pipeline (build → test → push → rolling restart).

## Layout

```
terraform/
  aws/
    providers.tf   AWS + Terraform version constraints
    variables.tf   region, cluster name, VPC CIDR, environment
    vpc.tf         VPC, public/private subnets, NAT gateway
    eks.tf         EKS cluster, node group, add-ons
    helm.tf        Traefik install via the Helm provider
    outputs.tf     cluster_endpoint, cluster_name, vpc_id
  azure/
    provider.tf    azurerm + helm provider, remote state backend
    variables.tf   resource group, location, cluster name, environment
    main.tf        resource group, VNet, subnet
    aks.tf         AKS cluster + node pool
    helm.tf        Traefik install via the Helm provider

k8s/
  deployment.yaml   2 replicas of the dashboard
  service.yaml      ClusterIP, 80 -> 8000
  ingress.yaml      Traefik, catches everything on /
  hpa.yaml          scales 2-4 replicas at 50% CPU
```

## What's in here

### AWS (EKS)

VPC across 2 AZs, private subnets for the cluster/nodes, public subnets for
a single NAT gateway (one NAT, not one per AZ — cheaper, and fine for what
this needs).

EKS 1.36 with the public endpoint on, one managed node group running
`t3.small`s (min 1, max 2, desired 2), and the standard add-ons —
`vpc-cni`, `coredns`, `kube-proxy`, and `metrics-server` (essential for the
HPA to work).

### Azure (AKS)

A VNet (`10.1.0.0/16`) with a single subnet dedicated to the node pool.
AKS 1.36.3 on the **Free** SKU tier — no hourly charge for the control
plane, just the node VMs — with one node pool of 2x `standard_b2ls_v2`,
`kubenet` networking, and a standard-SKU load balancer. Cluster identity is
system-assigned rather than a separate service principal.

State is stored remotely in Azure Blob Storage (`provider.tf`'s `backend
"azurerm"` block) rather than locally — see the state backend note under
Deploying, since that storage account has to exist *before* `terraform
init` will work, and Terraform won't create it for you.

Both clouds install Traefik the same way — through Terraform's Helm
provider rather than a separate `helm install` — so the ingress controller
lives in the same state as everything else.

### Kubernetes (either cloud)

`k8s/deployment.yaml`'s image field runs on 2 pods (256Mi/250m requested,
capped at 512Mi/500m), a ClusterIP service fronts it, Traefik ingress
routes all paths to that service, and the HPA scales up to 4 replicas past
50% average CPU. None of this changes based on which cloud it's running on.

## Choosing a cloud

One thing decides which cloud a pipeline run targets:

- **`TARGET_CLOUD`** — a repository **variable** (Settings > Secrets and
  variables > Actions > **Variables** tab — not Secrets, easy to set it in
  the wrong tab), value `aws` or `azure`. This is what a normal push to
  `main` reads.
- **Manual runs** (`workflow_dispatch`, on any of the three workflows below)
  instead show a dropdown to pick `azure`/`aws` for that one run, overriding
  `TARGET_CLOUD` without changing it.

This one switch controls two independent things:

- **`ci-cd.yml`** — after `build-and-push`, exactly one of `deploy-azure` /
  `deploy-aws` actually runs, based on the switch above.
- **`terraform-aws.yml`** / **`terraform-azure.yml`** — each already only
  triggers on changes under its own `terraform/aws/**` or
  `terraform/azure/**` path, but *also* checks the same switch before
  applying. Worth knowing: pushing a change to `terraform/azure/**` while
  `TARGET_CLOUD` is set to `aws` will run `plan`/`fmt` but **not apply** —
  the workflow triggers on the path, the apply gate checks the variable, and
  those are two different questions.

## Prereqs

Terraform >= 1.5.0 either way. For AWS: AWS CLI configured, kubectl. For
Azure: Azure CLI (`az login`), kubectl. Nothing exotic on the AWS side — 2
AZs, 1 NAT gateway, a couple `t3.small`s. Azure side needs an existing
storage account for remote state before `terraform init` (see below).

## GitHub Actions secrets

The pipeline (`.github/workflows/ci-cd.yml`, plus the two Terraform
workflows) needs these set under repo Settings > Secrets and variables >
Actions:

- `DOCKER_USERNAME` / `DOCKER_PASSWORD` — Docker Hub login, also used as the
  push target (`$DOCKER_USERNAME/options-scanner:latest`)
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — used to run
  `aws eks update-kubeconfig` and restart the deployment. Whatever IAM
  identity these belong to needs an EKS access entry on the cluster
  (`enable_cluster_creator_admin_permissions` only covers whoever ran
  `terraform apply`) — otherwise the rollout restart step fails with an
  auth error even though the credentials themselves are valid.
- `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` — Azure
  login for both the deploy job and `terraform-azure.yml`. No client
  secret: this is OIDC (`azure/login@v2` + `permissions: id-token: write`,
  already set on the relevant jobs). Adding these three secrets alone isn't
  enough — see "Azure OIDC setup" right below, since there's Azure-side
  configuration these values depend on.

### Azure OIDC setup (one-time)

GitHub authenticating to Azure via OIDC means there's no password to leak
in a secret — but it does mean Azure has to be told, ahead of time, exactly
which identity is allowed to log in and which GitHub repo/branch is allowed
to claim it. That's Azure-side setup, not a GitHub setting, and skipping it
is why login can fail even with all three secrets set correctly. From the
Azure CLI (`az login` first):

```bash
# 1. A dedicated identity for GitHub Actions to authenticate as
az identity create --name "github-actions-identity" \
  --resource-group "rg-options-scanner" --location israelcentral

# 2. Let it actually manage resources in the subscription
IDENTITY_PRINCIPAL_ID=$(az identity show --name "github-actions-identity" \
  --resource-group "rg-options-scanner" --query principalId -o tsv)
az role assignment create --assignee "$IDENTITY_PRINCIPAL_ID" \
  --role "Contributor" --scope "/subscriptions/$(az account show --query id -o tsv)"

# 3. The trust relationship: which repo/branch is allowed to use this identity
az identity federated-credential create \
  --name "github-actions-federated" \
  --identity-name "github-actions-identity" \
  --resource-group "rg-options-scanner" \
  --issuer "https://token.actions.githubusercontent.com" \
  --subject "repo:YOUR_GH_ORG/YOUR_REPO:ref:refs/heads/main"

# 4. The three values that become GitHub secrets
az identity show --name "github-actions-identity" \
  --resource-group "rg-options-scanner" --query clientId -o tsv   # AZURE_CLIENT_ID
az account show --query tenantId -o tsv                           # AZURE_TENANT_ID
az account show --query id -o tsv                                 # AZURE_SUBSCRIPTION_ID
```

The `--subject` in step 3 is the actual access control — it has to match
the OIDC token GitHub presents *exactly*, or the login is rejected
regardless of whether the three secrets are correct. `ref:refs/heads/main`
only matches pushes to `main`; it does **not** cover `pull_request`-triggered
runs, which present a different subject (`repo:org/repo:pull_request`).
`terraform-azure.yml` runs on both `push` and `pull_request` — as set up
above, only the `push` runs can authenticate; a PR-triggered `plan`/`fmt`
run will fail at login. A second federated credential with
`--subject "repo:YOUR_GH_ORG/YOUR_REPO:pull_request"` covers that case if
you want PR runs to actually plan rather than just fail fast.

One thing worth knowing: `k8s/deployment.yaml`'s image field defaults to
`itayb5/options-scanner:latest` and isn't read by the pipeline at all —
there's a comment right above it in the file if you're deploying your own
image (see Deploying below). The pipeline instead runs `kubectl set image`
with the secret on every deploy, which sets the live deployment's image
directly regardless of what's in the file.

`ci-cd.yml` also only triggers on pushes touching `app/**`, `Dockerfile`,
or `requirements.txt` — a Terraform-only change won't kick off a build, and
vice versa, the Terraform workflows won't fire on an app-only change.

Without the relevant secrets, the corresponding deploy job fails right at
login/auth — `test` still runs fine on its own since it doesn't touch
Docker Hub or either cloud.

## Deploying

**AWS:**

```bash
cd terraform/aws
terraform init
terraform plan
terraform apply
```

```bash
aws eks update-kubeconfig --region us-east-1 --name options-scanner-cluster
```

**Azure:** `provider.tf`'s `backend "azurerm"` block points state at:

- resource group: `rg-terraform-state`
- storage account: `tfstateos12345`
- container: `tfstate`
- state file: `azure.terraform.tfstate`

None of that gets created by `terraform init` or `apply` — it's the home
*for* the state, so it has to exist before Terraform has anywhere to write
to. One-time setup (storage account names are globally unique across all of
Azure, so `tfstateos12345` may need changing to something else if it's
taken — update `provider.tf` to match if so):

```bash
az group create --name rg-terraform-state --location israelcentral
az storage account create --name tfstateos12345 \
  --resource-group rg-terraform-state --sku Standard_LRS
az storage container create --name tfstate \
  --account-name tfstateos12345
```

Then, same as any other cloud's state:

```bash
cd terraform/azure
terraform init
terraform plan
terraform apply
```

```bash
az aks get-credentials --resource-group rg-options-scanner --name aks-options-scanner
```

Either way, if you're deploying your own image rather than
`itayb5/options-scanner`, change the `image:` line in `k8s/deployment.yaml`
first — there's a comment right above it. Then apply the manifests:

```bash
kubectl apply -f k8s/
```

Traefik takes a couple minutes to get its load balancer up after `apply`
finishes — check `kubectl get svc -n kube-system` for the external hostname.
Ingress doesn't filter by host, so hitting that hostname directly works too.

## Outputs

AWS (`terraform/aws/outputs.tf`):

- `cluster_endpoint` — EKS API server endpoint
- `cluster_name` — defaults to `options-scanner-cluster`
- `vpc_id`

Azure doesn't currently define an `outputs.tf` — `az aks get-credentials`
above pulls what you need directly from the resource group/cluster name.

## Variables

**AWS**, override via `terraform.tfvars` or `-var`:

- `aws_region` — `us-east-1`
- `cluster_name` — `options-scanner-cluster`
- `vpc_cidr` — `10.0.0.0/16`
- `environment` — `prod`

**Azure**:

- `resource_group_name` — `rg-options-scanner`
- `location` — `Israel Central`
- `cluster_name` — `aks-options-scanner`
- `environment` — `production`

## Tearing down

```bash
kubectl delete -f k8s/
cd terraform/aws        # or terraform/azure
terraform destroy
```

Delete the k8s resources first — otherwise Traefik's load balancer can end
up orphaned (an unmanaged AWS/Azure load balancer Terraform no longer knows
about) after the cluster's gone.
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name                             = var.cluster_name
  cluster_version                          = "1.36"
  enable_cluster_creator_admin_permissions = true
  cluster_endpoint_public_access           = true

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.private_subnets

  node_security_group_additional_rules = {
    ingress_metrics_server_10251 = {
      description                   = "Cluster API to node for metrics server"
      protocol                      = "tcp"
      from_port                     = 10251
      to_port                       = 10251
      type                          = "ingress"
      source_cluster_security_group = true
    }
  }

  cluster_addons = {
    metrics-server = {
      most_recent = true
    }
    vpc-cni = {
      most_recent = true
    }
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
  }

  eks_managed_node_groups = {
    app_nodes = {
      min_size     = 1
      max_size     = 2
      desired_size = 2

      instance_types = ["t3.small"]

      tags = {
        Environment = var.environment
        Terraform   = "true"
      }
    }
  }

  tags = {
    Environment = var.environment
    Terraform   = "true"
  }
}
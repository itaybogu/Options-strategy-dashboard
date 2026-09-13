variable "resource_group_name" {
  type        = string
  description = "The name of the Azure Resource Group"
  default     = "rg-options-scanner"
}

variable "location" {
  type        = string
  description = "Azure region for resources"
  default     = "Israel Central"
}

variable "cluster_name" {
  type        = string
  description = "The name of the Azure Kubernetes Service (AKS) cluster"
  default     = "aks-options-scanner"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
  default     = "production"
}
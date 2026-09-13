terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13.0"
    }
  }

  # Azure Blob Storage Remote State Backend

  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "tfstateos12345"
    container_name       = "tfstate"
    key                  = "azure.terraform.tfstate"
  }
}

provider "azurerm" {
  features {}
}
resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# Virtual Network (Equivalent to AWS VPC / OCI VCN)
resource "azurerm_virtual_network" "vnet" {
  name                = "vnet-options-scanner"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = ["10.1.0.0/16"]

  tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# Subnet dedicated for the AKS Node Pool
resource "azurerm_subnet" "aks_subnet" {
  name                 = "snet-aks-nodes"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.1.1.0/24"]
}
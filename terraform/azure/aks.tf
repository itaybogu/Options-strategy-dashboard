resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.cluster_name
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = "options-scanner-k8s"
  kubernetes_version  = "1.36.3"

  # Free tier cluster management (No Control Plane hourly cost)
  sku_tier = "Free"

  default_node_pool {
    name            = "appnodes"
    node_count      = 2
    vm_size         = "standard_b2ls_v2"
    vnet_subnet_id  = azurerm_subnet.aks_subnet.id
    os_disk_size_gb = 30

    upgrade_settings {
      max_surge                     = "10%"
      drain_timeout_in_minutes      = 0
      node_soak_duration_in_minutes = 0
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin    = "kubenet"
    load_balancer_sku = "standard"
  }

  tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}
targetScope = 'resourceGroup'

param location string = resourceGroup().location
@minLength(1)
param organizationName string
@minLength(2)
@maxLength(2)
param countryCode string
@minLength(1)
param industry string
param modelName string
param modelVersion string
@minValue(1)
param modelCapacity int = 1

var suffix = uniqueString(resourceGroup().id)
var pullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource foundry 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: 'pmo-ai-${suffix}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'pmo-ai-${suffix}'
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: foundry
  name: 'pmo'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {}
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: foundry
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'Anthropic'
      name: modelName
      version: modelVersion
    }
    modelProviderData: {
      organizationName: organizationName
      countryCode: countryCode
      industry: industry
    }
  }
  dependsOn: [project]
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: 'pmoacr${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    roleAssignmentMode: 'LegacyRegistryPermissions'
  }
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, project.id, pullRole)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: pullRole
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'pmost${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource conversations 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'pmo-conversations'
  properties: { publicAccess: 'None' }
}

output projectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output projectResourceId string = project.id
output modelDeploymentName string = model.name
output acrName string = registry.name
output image string = '${registry.properties.loginServer}/pmo-agent:1'
output storageAccountName string = storage.name
output storageAccountId string = storage.id
output foundryAccountId string = foundry.id
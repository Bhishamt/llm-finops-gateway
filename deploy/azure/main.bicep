// llm-finops-gateway on Azure Container Apps: the smallest shape that runs the gateway with
// its own identity (no provider key), a persistent ledger and a public HTTPS URL.
//
//   az deployment group create -g <rg> -f main.bicep -p image=<acr>.azurecr.io/llm-finops-gateway:main \
//     -p azureOpenAiResourceId=<id> -p azureOpenAiEndpoint=https://<name>.openai.azure.com/ \
//     -p deployments='{"gpt-4.1-mini":"gpt-4.1-mini-dev"}' -p keysYaml=@keys.yaml
//
// What it creates: Log Analytics + Container Apps environment, a storage account with an Azure
// Files share mounted at /data (SQLite ledger; use PostgreSQL for anything shared), a
// user-assigned identity with AcrPull on the registry and Cognitive Services OpenAI User on the
// Azure OpenAI resource, and the Container App. Team key hashes arrive as a secret file.

@description('Azure region; keep it next to the Azure OpenAI resource.')
param location string = resourceGroup().location

@description('Name prefix for every resource.')
param name string = 'lfg'

@description('Container image, e.g. <acr>.azurecr.io/llm-finops-gateway:main')
param image string

@description('Resource id of the Azure OpenAI account the gateway calls (role assignment target).')
param azureOpenAiResourceId string

@description('Azure OpenAI endpoint, https://<name>.openai.azure.com/')
param azureOpenAiEndpoint string

@description('JSON map model name -> deployment name, e.g. {"gpt-4.1-mini":"gpt-4.1-mini-dev"}')
param deployments string = '{}'

@description('Contents of config/keys.yaml (hashes only); mounted as a secret file.')
@secure()
param keysYaml string

@description('api_key (team keys from keysYaml) or headers (development only).')
@allowed(['api_key', 'headers'])
param authMode string = 'api_key'

@description('Display currencies for /finops, e.g. {"EUR":0.88}')
param reportRates string = '{}'

@description('Optional Slack/Teams webhook for budget and upstream alerts.')
@secure()
param alertWebhookUrl string = ''

@description('0 = scale to zero when idle (first request takes a few seconds), 1 = always on.')
@minValue(0)
@maxValue(3)
param minReplicas int = 1

var registryServer = split(image, '/')[0]
var openAiRg = split(azureOpenAiResourceId, '/')[4]
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: toLower(replace('${name}${uniqueString(resourceGroup().id)}', '-', ''))
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource files 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: files
  name: 'ledger'
  properties: { shareQuota: 5 }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'ledger'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-identity'
  location: location
}

// Pull from the registry in this resource group with the identity, no admin user needed.
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: split(registryServer, '.')[0]
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Call Azure OpenAI with the identity (ADR-0014); the account may live in another resource group.
module openAiRole 'openai-role.bicep' = {
  name: '${name}-openai-role'
  scope: resourceGroup(openAiRg)
  params: {
    azureOpenAiResourceId: azureOpenAiResourceId
    principalId: identity.properties.principalId
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-gateway'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        { server: registryServer, identity: identity.id }
      ]
      secrets: [
        { name: 'keys-yaml', value: keysYaml }
        { name: 'alert-webhook', value: empty(alertWebhookUrl) ? 'unset' : alertWebhookUrl }
      ]
    }
    template: {
      containers: [
        {
          name: 'gateway'
          image: image
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: concat(
            [
              { name: 'LFG_PROVIDER', value: 'azure_openai' }
              { name: 'LFG_AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
              { name: 'LFG_AZURE_OPENAI_AUTH', value: 'entra' }
              { name: 'LFG_AZURE_OPENAI_DEPLOYMENTS', value: deployments }
              { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
              { name: 'LFG_AUTH_MODE', value: authMode }
              { name: 'LFG_KEYS_PATH', value: '/secrets/keys.yaml' }
              { name: 'LFG_DATABASE_URL', value: 'sqlite:////data/gateway.db' }
              { name: 'LFG_REPORT_RATES', value: reportRates }
            ],
            empty(alertWebhookUrl) ? [] : [ { name: 'LFG_ALERT_WEBHOOK_URL', secretRef: 'alert-webhook' } ]
          )
          volumeMounts: [
            { volumeName: 'ledger', mountPath: '/data' }
            { volumeName: 'secrets', mountPath: '/secrets' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: [
        { name: 'ledger', storageType: 'AzureFile', storageName: envStorage.name }
        {
          name: 'secrets'
          storageType: 'Secret'
          secrets: [ { secretRef: 'keys-yaml', path: 'keys.yaml' } ]
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: 1 }
    }
  }
  dependsOn: [ acrPull, openAiRole ]
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output identityPrincipalId string = identity.properties.principalId

// The LiteLLM direction next to an existing gateway deployment (main.bicep): a LiteLLM proxy
// with the FinOps plugin, the report page as a LiteLLM dashboard plugin, and the assistant
// example, all in the environment main.bicep created. Config files (price book, policies,
// LiteLLM config) live on an Azure Files share mounted read-only into the proxy and the
// report; push-config.sh uploads them and the hot reload (ADR-0022) picks them up.
//
//   az deployment group create -g <rg> -f deploy/azure/litellm.bicep -p ...   (see litellm-deploy.sh)

@description('Region of the environment')
param location string = resourceGroup().location

@description('Name prefix shared with main.bicep (environment <name>-env, identity <name>-identity)')
param name string = 'lfg'

@description('Registry login server, e.g. lfgacr1325.azurecr.io (the identity already has AcrPull)')
param registryServer string

@description('LiteLLM image with the plugin installed (examples/litellm/Dockerfile)')
param litellmImage string

@description('The gateway image, used for the report service')
param gatewayImage string

@description('The assistant image (examples/assistant/Dockerfile)')
param assistantImage string

@description('Storage account of the environment (the config share is created on it)')
param storageAccountName string

@description('PostgreSQL Flexible Server name; the litellm database is created on it')
param postgresServerName string

@secure()
@description('LiteLLM database URL (postgresql://user:password@host/litellm?sslmode=require)')
param litellmDatabaseUrl string

@secure()
@description('Ledger database URL for the plugin and the report (the gateway database-url secret)')
param ledgerDatabaseUrl string

@secure()
param litellmMasterKey string

@secure()
param litellmSaltKey string

@description('Azure OpenAI endpoint, e.g. https://<account>.openai.azure.com/')
param azureOpenAiEndpoint string

@secure()
param azureOpenAiApiKey string

param azureOpenAiApiVersion string = '2024-10-21'

@secure()
@description('keys.yaml of the report service (team keys and a finops key, hashes only)')
param keysYaml string

@secure()
@description('A raw role: finops key the assistant uses for /v1/finops/budgets')
param reportKey string

@description('Display currencies for the report, e.g. {"EUR":0.88}')
param reportRates string = '{}'

@description('Model names offered by the assistant when the policy allows any')
param assistantModels string = 'gpt-4.1-mini,gpt-5-mini'

var configShareName = 'finops-config'

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: '${name}-env'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: '${name}-identity'
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource files 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource configShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: files
  name: configShareName
  properties: { shareQuota: 1 }
}

resource envConfigStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: configShareName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: configShare.name
      accessMode: 'ReadOnly'
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' existing = {
  name: postgresServerName
}

resource litellmDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgres
  name: 'litellm'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

var configVolume = {
  name: 'config'
  storageType: 'AzureFile'
  storageName: envConfigStorage.name
}
var registries = [ { server: registryServer, identity: identity.id } ]
var identityBlock = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identity.id}': {} }
}

// 1. LiteLLM proxy with the FinOps plugin (port 4000; the dashboard at /ui)
resource litellm 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-litellm'
  location: location
  identity: identityBlock
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 4000, transport: 'http', allowInsecure: false }
      registries: registries
      secrets: [
        { name: 'litellm-database-url', value: litellmDatabaseUrl }
        { name: 'ledger-database-url', value: ledgerDatabaseUrl }
        { name: 'master-key', value: litellmMasterKey }
        { name: 'salt-key', value: litellmSaltKey }
        { name: 'azure-openai-key', value: azureOpenAiApiKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'litellm'
          image: litellmImage
          command: [ 'litellm', '--config', '/config/litellm.yaml', '--port', '4000' ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'DATABASE_URL', secretRef: 'litellm-database-url' }
            { name: 'LITELLM_MASTER_KEY', secretRef: 'master-key' }
            { name: 'LITELLM_SALT_KEY', secretRef: 'salt-key' }
            { name: 'AZURE_API_BASE', value: azureOpenAiEndpoint }
            { name: 'AZURE_API_KEY', secretRef: 'azure-openai-key' }
            { name: 'AZURE_API_VERSION', value: azureOpenAiApiVersion }
            { name: 'LFG_PRICING_PATH', value: '/config/pricing.yaml' }
            { name: 'LFG_POLICIES_PATH', value: '/config/policies.yaml' }
            { name: 'LFG_DATABASE_URL', secretRef: 'ledger-database-url' }
          ]
          volumeMounts: [ { volumeName: 'config', mountPath: '/config' } ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/liveliness', port: 4000 }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: [ configVolume ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ litellmDb ]
}

// 2. The report page over the shared ledger, served as a LiteLLM dashboard plugin (ADR-0024)
resource report 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-report'
  location: location
  identity: identityBlock
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http', allowInsecure: false }
      registries: registries
      secrets: [
        { name: 'keys-yaml', value: keysYaml }
        { name: 'ledger-database-url', value: ledgerDatabaseUrl }
        { name: 'salt-key', value: litellmSaltKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'report'
          image: gatewayImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'LFG_AUTH_MODE', value: 'api_key' }
            { name: 'LFG_KEYS_PATH', value: '/secrets/keys.yaml' }
            { name: 'LFG_PRICING_PATH', value: '/config/pricing.yaml' }
            { name: 'LFG_POLICIES_PATH', value: '/config/policies.yaml' }
            { name: 'LFG_DATABASE_URL', secretRef: 'ledger-database-url' }
            { name: 'LFG_REPORT_RATES', value: reportRates }
            { name: 'LFG_LITELLM_PLUGIN_NAME', value: 'finops' }
            { name: 'LFG_LITELLM_SALT_KEY', secretRef: 'salt-key' }
          ]
          volumeMounts: [
            { volumeName: 'secrets', mountPath: '/secrets' }
            { volumeName: 'config', mountPath: '/config' }
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
        {
          name: 'secrets'
          storageType: 'Secret'
          secrets: [ { secretRef: 'keys-yaml', path: 'keys.yaml' } ]
        }
        configVolume
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// 3. The assistant example against the proxy, budgets from the report
resource assistant 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-assistant'
  location: location
  identity: identityBlock
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8501, transport: 'http', allowInsecure: false }
      registries: registries
      secrets: [ { name: 'report-key', value: reportKey } ]
    }
    template: {
      containers: [
        {
          name: 'assistant'
          image: assistantImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'LFG_ASSISTANT_TARGET_URL', value: 'https://${litellm.properties.configuration.ingress.fqdn}' }
            { name: 'LFG_ASSISTANT_REPORT_URL', value: 'https://${report.properties.configuration.ingress.fqdn}' }
            { name: 'LFG_ASSISTANT_REPORT_KEY', secretRef: 'report-key' }
            { name: 'LFG_ASSISTANT_MODELS', value: assistantModels }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output litellmUrl string = 'https://${litellm.properties.configuration.ingress.fqdn}'
output reportUrl string = 'https://${report.properties.configuration.ingress.fqdn}'
output assistantUrl string = 'https://${assistant.properties.configuration.ingress.fqdn}'
output configShare string = configShareName

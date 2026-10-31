// Grants "Cognitive Services OpenAI User" on one Azure OpenAI account to a principal.
// Deployed as a module at the account's resource group, so the account may live anywhere.

param azureOpenAiResourceId string
param principalId string

var roleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: last(split(azureOpenAiResourceId, '/'))
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, roleId)
  scope: account
  properties: {
    roleDefinitionId: roleId
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

targetScope = 'resourceGroup'

// ── Parameters ─────────────────────────────────────────────────────────────────
@description('AZD environment name – used as a prefix for all resource names.')
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Azure AI Project endpoint URL (from AI Foundry).')
param azureAiProjectEndpoint string = ''

@description('Azure AI model deployment name.')
param azureAiModelDeployment string = 'gpt-4.1'

@description('App Service Plan SKU. B1 = Basic (recommended); F1 = Free (cold-start issues).')
param appServicePlanSku string = 'B1'

@description('Pre-registered AI Foundry agent ID. When set, bypasses list_agents/create_agent at startup.')
param agentId string = ''

// ── Variables ──────────────────────────────────────────────────────────────────
var suffix         = uniqueString(resourceGroup().id)
var webAppName     = 'aiterms-${suffix}'
var planName       = '${environmentName}-plan'
var logWorkspace   = '${environmentName}-logs'
var appInsightsName = '${environmentName}-insights'

// ── Log Analytics Workspace ────────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logWorkspace
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ── Application Insights ───────────────────────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── App Service Plan (Linux) ───────────────────────────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: planName
  location: location
  kind: 'linux'
  sku: {
    name: appServicePlanSku
    tier: appServicePlanSku == 'F1' ? 'Free' : (appServicePlanSku == 'B1' ? 'Basic' : 'Standard')
  }
  properties: {
    reserved: true   // required for Linux
  }
}

// ── Web App ────────────────────────────────────────────────────────────────────
resource webApp 'Microsoft.Web/sites@2023-01-01' = {
  name: webAppName
  location: location
  tags: {
    // AZD uses these tags to identify which resource to deploy the 'web' service to
    'azd-service-name': 'web'
    'azd-env-name': environmentName
  }
  identity: {
    type: 'SystemAssigned'   // enables passwordless auth to Azure AI Foundry
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'bash startup.sh'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      appSettings: [
        { name: 'AZURE_AI_PROJECT_ENDPOINT',            value: azureAiProjectEndpoint }
        { name: 'AZURE_AI_MODEL_DEPLOYMENT',             value: azureAiModelDeployment }
        { name: 'AGENT_NAME',                            value: 'AITermsScoreAgent' }
        { name: 'AGENT_ID',                              value: agentId }
        { name: 'APPINSIGHTS_INSTRUMENTATIONKEY',        value: appInsights.properties.InstrumentationKey }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT',        value: 'true' }
        { name: 'WEBSITES_PORT',                         value: '8000' }
        // Tells Oryx (App Service build system) which pip requirements file to use
        { name: 'PRE_BUILD_COMMAND',                     value: '' }
        { name: 'POST_BUILD_COMMAND',                    value: '' }
      ]
    }
  }
}

// ── Outputs ────────────────────────────────────────────────────────────────────
@description('Public URL of the deployed web app.')
output webAppUrl string = 'https://${webApp.properties.defaultHostName}'

@description('Web app resource name (for azd deploy targeting).')
output webAppName string = webApp.name

@description('Principal ID of the system-assigned managed identity – use this to assign Azure AI Developer role.')
output managedIdentityPrincipalId string = webApp.identity.principalId

using './main.bicep'

param location = 'swedencentral'
param modelFormat = 'OpenAI'
param modelName = readEnvironmentVariable('PMO_GPT_CATALOG_MODEL')
param modelVersion = readEnvironmentVariable('PMO_GPT_MODEL_VERSION')
param deploymentName = 'pmo-gpt'
param modelCapacity = 1
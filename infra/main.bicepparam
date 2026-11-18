using './main.bicep'

param location = 'swedencentral'
param organizationName = readEnvironmentVariable('CLAUDE_ORGANIZATION')
param countryCode = readEnvironmentVariable('CLAUDE_COUNTRY_CODE')
param industry = readEnvironmentVariable('CLAUDE_INDUSTRY')
param modelName = 'claude-opus-5-5'
param modelVersion = '1'
param modelCapacity = 1
<#
.SYNOPSIS
  Creates the Azure VM for JenkinsGuardian: one Ubuntu machine, SSH only from your IP, HTTP/HTTPS open, and a free DNS name.

.DESCRIPTION
  UNTESTED: written from the Azure CLI documentation, not run against a subscription. Read each command before running it and fix this file
  when a flag turns out different.

  Needs the Azure CLI (`winget install Microsoft.AzureCLI`) and a signed-in session (`az login`).
  Find a region your Azure for Students subscription allows: `az account list-locations -o table`, or the region list in the portal when
  you create a resource group (student subscriptions are usually limited to a few). If the size is "not available" there, try another
  allowed region or `-Size Standard_D4s_v5`.

  Cost: Standard_B4ms (4 vCPU, 16 GB) is roughly 0.15-0.17 USD per hour: check the Azure pricing calculator. A DEALLOCATED VM costs no
  compute, only its disk, so stop it when you are not using it (see README.md).

.EXAMPLE
  .\deploy\azure\create_vm.ps1 -Location westeurope -MyIp (Invoke-RestMethod https://api.ipify.org)
#>
param(
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$MyIp,
    [string]$Name = "jguardian",
    [string]$Size = "Standard_B4ms",
    [int]$DiskGb = 64
)

$ErrorActionPreference = "Stop"
$group = "$Name-rg"
$dnsLabel = "$Name-$(Get-Random -Minimum 10000 -Maximum 99999)"   # must be unique inside the region; becomes <label>.<region>.cloudapp.azure.com

az account show --query "{subscription:name, state:state}" -o table
if ($LASTEXITCODE -ne 0) { throw "Run 'az login' first." }

Write-Host "`n[1/4] resource group $group in $Location"
az group create --name $group --location $Location -o none

Write-Host "[2/4] VM $Name ($Size, Ubuntu 22.04, ${DiskGb} GB disk) with DNS label $dnsLabel"
az vm create `
    --resource-group $group --name $Name `
    --image Ubuntu2204 --size $Size `
    --admin-username azureuser --generate-ssh-keys `
    --os-disk-size-gb $DiskGb `
    --public-ip-sku Standard --public-ip-address-dns-name $dnsLabel `
    --nsg-rule SSH `
    -o none

Write-Host "[3/4] SSH only from $MyIp; ports 80 and 443 for the web (nothing else is opened)"
$nsg = az network nsg list --resource-group $group --query "[0].name" -o tsv
az network nsg rule update --resource-group $group --nsg-name $nsg --name default-allow-ssh --source-address-prefixes "$MyIp/32" -o none
az vm open-port --resource-group $group --name $Name --port 80  --priority 900 -o none
az vm open-port --resource-group $group --name $Name --port 443 --priority 901 -o none

Write-Host "[4/4] done"
$fqdn = az vm show -d --resource-group $group --name $Name --query fqdns -o tsv
$ip = az vm show -d --resource-group $group --name $Name --query publicIps -o tsv
Write-Host "`nVM address : $fqdn   ($ip)"
Write-Host "SSH        : ssh azureuser@$fqdn"
Write-Host "Next       : follow deploy/azure/README.md step 2"
Write-Host "Stop billing when idle:  az vm deallocate --resource-group $group --name $Name"
Write-Host "Delete everything:       az group delete --name $group --yes --no-wait"

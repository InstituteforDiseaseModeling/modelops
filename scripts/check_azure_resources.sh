#!/bin/bash
# Script to check Azure resources and compare with expected naming

echo "=== Current Azure Resources ==="
echo ""

# Get environment and username
ENV="${1:-dev}"
USERNAME="${USER:-$USERNAME}"
USERNAME_CLEAN=$(echo $USERNAME | tr -d '.-' | tr '[:upper:]' '[:lower:]' | cut -c1-20)

echo "Environment: $ENV"
echo "Username: $USERNAME (cleaned: $USERNAME_CLEAN)"
echo ""

# Expected names from centralized naming
EXPECTED_RG="modelops-$ENV-rg-$USERNAME_CLEAN"
EXPECTED_AKS="modelops-$ENV-aks"

echo "=== Expected Names (from StackNaming) ==="
echo "Resource Group: $EXPECTED_RG"
echo "AKS Cluster: $EXPECTED_AKS"
echo ""

echo "=== Actual Azure Resources ==="
echo ""

# Check resource groups
echo "Resource Groups:"
az group list --query "[?contains(name, 'modelops')].{Name:name, Location:location}" --output table

echo ""
echo "AKS Clusters in modelops-rg-$USERNAME_CLEAN:"
az aks list --resource-group "modelops-rg-$USERNAME_CLEAN" --query "[].{Name:name, Version:kubernetesVersion, State:provisioningState}" --output table 2>/dev/null || echo "  No clusters found or resource group doesn't exist"

echo ""
echo "=== Analysis ==="
echo ""

# Check if actual RG matches expected
ACTUAL_RG="modelops-rg-$USERNAME_CLEAN"
if [[ "$ACTUAL_RG" != "$EXPECTED_RG" ]]; then
    echo "⚠️  Resource Group Mismatch:"
    echo "   Actual:   $ACTUAL_RG"
    echo "   Expected: $EXPECTED_RG"
    echo ""
fi

# Get actual AKS name
ACTUAL_AKS=$(az aks list --resource-group "modelops-rg-$USERNAME_CLEAN" --query "[0].name" --output tsv 2>/dev/null)
if [[ -n "$ACTUAL_AKS" ]]; then
    echo "⚠️  AKS Cluster Name Mismatch:"
    echo "   Actual:   $ACTUAL_AKS"
    echo "   Expected: $EXPECTED_AKS"
    echo ""
    echo "The AKS cluster was created with a different naming pattern."
    echo "This is why 'mops infra status' cannot find it."
else
    echo "No AKS cluster found in the resource group."
fi

echo ""
echo "=== Recommendations ==="
echo ""
echo "Option 1: Keep existing cluster and import to Pulumi (advanced)"
echo "  - Would require manual import commands"
echo ""
echo "Option 2: Recreate with correct naming (recommended for dev)"
echo "  1. Delete existing AKS cluster:"
echo "     az aks delete --name $ACTUAL_AKS --resource-group modelops-rg-$USERNAME_CLEAN --yes --no-wait"
echo "  2. Recreate with ModelOps:"
echo "     mops infra up --config <your-config> --env $ENV"
echo ""
echo "Option 3: Update your config to use existing name"
echo "  - Set aks.name: $ACTUAL_AKS in your azure.yaml"
echo "  - But this won't follow the new centralized naming"
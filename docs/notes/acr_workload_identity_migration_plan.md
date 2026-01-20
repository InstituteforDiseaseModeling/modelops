# ACR Workload Identity Migration Plan

## Current State

The ModelOps infrastructure currently uses **ACR admin user credentials** for container registry authentication:

- **File**: `modelops/src/modelops/infra/components/registry.py`
- **Line 130**: `admin_user_enabled=True`
- **Lines 144-166**: Admin credentials retrieved and stored in Kubernetes secrets

### Current Flow
1. Registry component enables ACR admin user
2. Admin username/password retrieved via `list_registry_credentials_output()`
3. Credentials stored in K8s Secret `bundle-credentials`
4. Workers mount secret as environment variables (`REGISTRY_USERNAME`, `REGISTRY_PASSWORD`)
5. Workers authenticate to ACR using basic auth

### Security Concerns
- Admin credentials are long-lived and powerful
- Credentials stored in multiple places (Pulumi state, K8s secrets, worker env)
- No automatic rotation
- Over-privileged (admin has push/delete, workers only need pull)

## Target State: Azure Workload Identity

Use Azure AD Workload Identity to authenticate pods to ACR without explicit credentials.

### Benefits
- No secrets to manage or rotate
- Principle of least privilege (can scope to AcrPull only)
- Short-lived tokens (refreshed automatically)
- Audit trail via Azure AD logs

## Migration Steps

### Phase 1: Enable AKS Workload Identity (Infrastructure)

1. **Update AKS cluster configuration** (`azure.py`):
   ```python
   # Add to ManagedCluster args
   oidc_issuer_profile=azure.containerservice.ManagedClusterOIDCIssuerProfileArgs(
       enabled=True,
   ),
   security_profile=azure.containerservice.ManagedClusterSecurityProfileArgs(
       workload_identity=azure.containerservice.ManagedClusterSecurityProfileWorkloadIdentityArgs(
           enabled=True,
       ),
   ),
   ```

2. **Create User-Assigned Managed Identity** (new in `registry.py`):
   ```python
   identity = azure.managedidentity.UserAssignedIdentity(
       f"{name}-acr-pull-identity",
       resource_group_name=rg_name,
       location=location,
   )
   ```

3. **Grant AcrPull role to the identity**:
   ```python
   azure.authorization.RoleAssignment(
       f"{name}-acr-pull",
       principal_id=identity.principal_id,
       role_definition_id=acr_pull_role_id,
       scope=acr.id,
   )
   ```

4. **Create federated credential** linking K8s service account to Azure identity:
   ```python
   azure.managedidentity.FederatedIdentityCredential(
       f"{name}-fed-cred",
       resource_group_name=rg_name,
       identity_name=identity.name,
       issuer=cluster.oidc_issuer_profile.issuer_url,
       subject=f"system:serviceaccount:{namespace}:dask-worker",
       audiences=["api://AzureADTokenExchange"],
   )
   ```

### Phase 2: Update Workspace Deployment

1. **Create annotated service account** (`workspace.py`):
   ```python
   k8s.core.v1.ServiceAccount(
       "dask-worker-sa",
       metadata=k8s.meta.v1.ObjectMetaArgs(
           name="dask-worker",
           namespace=namespace,
           annotations={
               "azure.workload.identity/client-id": identity_client_id,
           },
       ),
   )
   ```

2. **Update worker deployment** to use service account and workload identity labels:
   ```python
   # Pod spec additions
   service_account_name="dask-worker",
   metadata=k8s.meta.v1.ObjectMetaArgs(
       labels={
           "azure.workload.identity/use": "true",
       },
   ),
   ```

3. **Remove credential environment variables** from worker deployment:
   - Remove `REGISTRY_USERNAME`
   - Remove `REGISTRY_PASSWORD`
   - Remove `bundle-credentials` secret mount

### Phase 3: Update Bundle Repository Client

1. **Update `modelops-bundle` authentication** to support workload identity:
   - Detect when running in AKS with workload identity
   - Use Azure Identity SDK to get token
   - Exchange token for ACR refresh token

2. **Code changes in bundle repository**:
   ```python
   from azure.identity import DefaultAzureCredential

   def get_acr_token(registry: str) -> str:
       credential = DefaultAzureCredential()
       # DefaultAzureCredential will use workload identity in AKS
       token = credential.get_token(f"https://{registry}/.default")
       return exchange_for_acr_refresh_token(token)
   ```

### Phase 4: Deprecation & Cleanup

1. **Feature flag for transition period**:
   ```yaml
   # modelops.yaml
   registry:
     auth_method: "workload_identity"  # or "admin_user" for backwards compat
   ```

2. **Remove admin user** after validation:
   ```python
   admin_user_enabled=False,  # Secure default
   ```

3. **Remove legacy credential code**:
   - Delete `_get_admin_credentials()` method
   - Remove `bundle-credentials` secret creation
   - Update documentation

## Testing Strategy

1. **Local development**: Continue using Azurite + local registry (no auth)
2. **Dev environment**: Deploy with workload identity, test bundle operations
3. **Staging**: Full integration testing with realistic workloads
4. **Rollback plan**: Keep admin user code behind feature flag for 1 release

## Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|--------------|
| Phase 1: Infrastructure | 4-6 hours | AKS version >= 1.27 |
| Phase 2: Workspace | 2-4 hours | Phase 1 complete |
| Phase 3: Bundle client | 4-6 hours | azure-identity package |
| Phase 4: Cleanup | 2 hours | All phases validated |

**Total**: ~2 days of focused work

## References

- [Azure Workload Identity](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview)
- [ACR Authentication with Managed Identity](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity)
- [Pulumi Azure Workload Identity](https://www.pulumi.com/registry/packages/azure-native/api-docs/managedidentity/)

## Action Items

- [ ] Verify AKS cluster version supports workload identity (>= 1.27)
- [ ] Update `azure.py` to enable OIDC issuer and workload identity
- [ ] Create managed identity and federated credential in `registry.py`
- [ ] Update `workspace.py` to use annotated service account
- [ ] Update `modelops-bundle` authentication to use DefaultAzureCredential
- [ ] Test full workflow in dev environment
- [ ] Document rollback procedure
- [ ] Remove admin user after validation period

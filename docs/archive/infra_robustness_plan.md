# Infrastructure Provisioning Robustness Plan

## Problem Statement
Partial infrastructure failures leave the system in inconsistent states where components report "READY" but their outputs are unavailable to dependent components.

## Root Causes
1. **State checking is shallow**: Only checks component exists, not output availability
2. **No retry logic for output retrieval**: StackReferences fail if outputs missing
3. **Dependency graph not enforced**: Components can provision out of order

## Proposed Solution

### 1. Enhanced State Validation
```python
# src/modelops/infra/components/base.py
class ComponentState:
    """Enhanced state with output validation"""

    async def is_truly_ready(self, stack: auto.Stack) -> bool:
        """Check both existence and output availability"""
        if self.status != ComponentStatus.READY:
            return False

        # Verify critical outputs exist
        try:
            outputs = await stack.outputs()
            required = self.get_required_outputs()
            return all(k in outputs for k in required)
        except Exception:
            return False
```

### 2. Dependency-Aware Provisioning
```python
# src/modelops/infra/provisioner.py
class DependencyGraph:
    """Explicit dependency ordering"""

    def __init__(self):
        self.components = {
            'cluster': {'depends_on': []},
            'storage': {'depends_on': []},
            'workspace': {'depends_on': ['cluster', 'storage']},
            'adaptive': {'depends_on': ['workspace']}
        }

    def provision_order(self, target: str) -> list[str]:
        """Return components in dependency order"""
        # Topological sort implementation
        visited = set()
        order = []

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            for dep in self.components[name]['depends_on']:
                visit(dep)
            order.append(name)

        visit(target)
        return order
```

### 3. Retry Logic for Stack References
```python
# src/modelops/infra/components/workspace.py
async def get_infra_outputs_with_retry(
    self,
    max_retries: int = 3,
    delay: float = 2.0
) -> dict:
    """Get infrastructure outputs with exponential backoff"""
    for attempt in range(max_retries):
        try:
            infra_ref = StackReference(
                f"modelops-infra-{self.env}",
                workspace=self.workspace
            )

            # Try to get all required outputs
            outputs = {
                'kubeconfig': await infra_ref.get_output('kubeconfig'),
                'cluster_name': await infra_ref.get_output('cluster_name'),
                'namespace': await infra_ref.get_output('namespace')
            }

            # Verify outputs are not None
            if all(v is not None for v in outputs.values()):
                return outputs

        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                raise RuntimeError(
                    f"Failed to get infrastructure outputs after {max_retries} attempts: {e}"
                )
```

### 4. Idempotent Component Recreation
```python
# src/modelops/cli/infra.py
@infra.command()
@click.option('--force-recreate', is_flag=True, help='Force recreation of components')
async def up(ctx, config: str, force_recreate: bool):
    """Provision infrastructure with recovery logic"""

    if force_recreate:
        # Tear down in reverse dependency order
        for component in reversed(graph.provision_order('workspace')):
            await tear_down_component(component)

    # Provision in dependency order
    for component in graph.provision_order('workspace'):
        state = await get_component_state(component)

        if not await state.is_truly_ready():
            console.print(f"[yellow]Component {component} needs provisioning[/yellow]")
            await provision_component(component)
        else:
            console.print(f"[green]Component {component} is ready[/green]")
```

### 5. Health Check Endpoints
```python
# src/modelops/infra/health.py
class InfraHealthCheck:
    """Verify all components are functional"""

    async def check_all(self) -> dict[str, bool]:
        return {
            'cluster': await self.check_cluster(),
            'storage': await self.check_storage(),
            'workspace': await self.check_workspace()
        }

    async def check_cluster(self) -> bool:
        """Verify AKS cluster is accessible"""
        try:
            # Try to list nodes
            v1 = client.CoreV1Api()
            nodes = v1.list_node(timeout_seconds=5)
            return len(nodes.items) > 0
        except Exception:
            return False
```

## Implementation Priority

### Phase 1 (Quick Fixes)
1. Add `--force-recreate` flag to `mops infra up`
2. Improve error messages when stack outputs missing
3. Add retry logic to StackReference calls

### Phase 2 (Medium Term)
1. Implement dependency graph provisioning
2. Add health check validation
3. Enhanced state validation with output checks

### Phase 3 (Long Term)
1. Automatic recovery from partial failures
2. Rollback capabilities
3. State reconciliation loops

## CLI Usage After Implementation
```bash
# Force recreation if in bad state
mops infra up --config azure.yaml --force-recreate

# Check health status
mops infra health

# Repair specific component
mops infra repair workspace

# Validate all dependencies met
mops infra validate
```

## Estimated Effort
- Phase 1: 2-3 hours (quick wins)
- Phase 2: 1-2 days (solid recovery)
- Phase 3: 3-5 days (full resilience)

## Recommendation
Implement Phase 1 now for immediate relief, defer Phase 2/3 until after smoke test validation proves the core system works end-to-end.
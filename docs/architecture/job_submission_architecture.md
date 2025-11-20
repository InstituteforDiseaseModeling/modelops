# Job Submission Architecture Analysis

## You're Right - Separation of Concerns is Critical

Let me analyze why keeping sampling separate from bundle management is the correct design choice:

### Current Clean Separation (Calabaria + ModelOps bundle)

```bash
# Clear, focused commands
cb sampling sobol --model seir --n 1000 > job.json
cb sampling grid --model seir --grid-points 10 > job.json

# Bundle management stays in modelops-bundle + mops
modelops-bundle register-model src/models/seir.py
mops jobs submit job.json --bundle sha256:abc...   # or rely on --auto (default)
```

This is **good design** - Calabaria handles experimental design, while
bundle registration/publishing happens through `modelops-bundle` and the
`mops jobs submit --auto` flow.

### Why SimJob Needs bundle_ref (But Maybe Shouldn't)

Looking at the contracts:

```python
@dataclass(frozen=True)
class SimTask:
    bundle_ref: str  # Required! Why?
    entrypoint: str
    params: UniqueParameterSet
    seed: int
```

The bundle_ref is there because:
1. **Version pinning** - Which exact code version to run
2. **Distribution** - Workers need to know what to pull
3. **Caching** - Workers can cache pulled bundles

But you're right to question this - **sampling doesn't care about bundles**, it cares about:
- Parameter space exploration
- Statistical methods
- Experimental design

### Alternative Designs

#### Option 1: Late Binding (Sampling Doesn't Know Bundle)
```python
# Sampling generates "abstract" job
class AbstractSimJob:
    entrypoint: str  # Just the logical name
    params: List[Params]
    sampling_method: str
    # NO bundle_ref!

# Submission binds to bundle
mops jobs submit job.json --bundle-ref sha256:abc...
# OR auto-detect latest bundle
mops jobs submit job.json --bundle latest
```

**Pros**:
- Clean separation
- Can reuse same job with different code versions
- Sampling tool stays pure (just math)

**Cons**:
- Two-step process
- Could submit with wrong/missing bundle

#### Option 2: Bundle Discovery (Convention Over Configuration)
```python
# SimJob has optional bundle_ref
class SimTask:
    bundle_ref: Optional[str] = None  # Can be filled later

# At submission time
if not job.bundle_ref:
    job.bundle_ref = find_latest_bundle(job.entrypoint)
```

**Pros**:
- Single job file works
- Can override if needed
- Sampling doesn't need to know

**Cons**:
- "Latest" is ambiguous
- Reproducibility issues

#### Option 3: Bundle Manifest Binding
```bash
# Manifest declares available bundles
manifest.yaml:
  models:
    seir:
      current_bundle: sha256:abc...
      entrypoints: [baseline, intervention]

# Sampling reads manifest
cb sampling sobol --manifest manifest.yaml --model seir

# Job gets bundle from manifest
job.json:
  entrypoint: seir/baseline
  bundle_ref: sha256:abc...  # Auto-filled from manifest
```

**Pros**:
- Single source of truth
- Sampling tool can validate entrypoints exist
- Still separated concerns

**Cons**:
- Requires manifest updates
- More complex workflow

### The Real Problem: Contract Design

The core issue is SimTask **requires** bundle_ref when it should perhaps be:

```python
# Better contract design?
@dataclass
class SimTask:
    entrypoint: str
    params: UniqueParameterSet
    seed: int
    # bundle_ref moved elsewhere!

@dataclass
class ExecutableSimTask:
    task: SimTask
    bundle_ref: str  # Binding happens here
```

This would allow sampling to generate pure SimTasks without bundle knowledge.

### Trade-off Analysis

| Approach | Separation | Reproducibility | Developer UX | Error Potential |
|----------|------------|-----------------|--------------|-----------------|
| **Current (bundle required)** | Poor | Perfect | Annoying | Low |
| **Late binding** | Perfect | Good | Clean | Medium |
| **Discovery** | Good | Poor | Magic | High |
| **Manifest binding** | Good | Perfect | Verbose | Low |

### My Recommendation: Late Binding with Defaults

```bash
# Sampling generates pure job (no bundle)
cb sampling sobol --model seir --n 1000 > job.json

# Submission has multiple options:

# 1. Explicit bundle (reproducible)
mops jobs submit job.json --bundle sha256:abc...

# 2. Latest from registry (convenient)
mops jobs submit job.json --bundle latest

# 3. Local development (fast iteration)
mops jobs submit job.json --bundle local:./path

# 4. Auto-build and submit (consolidated)
mops jobs submit job.json --build
```

### Implementation Path

1. **Modify SimJob contract** to make bundle_ref optional:
   ```python
   class SimJob:
       bundle_ref: Optional[str] = None
   ```

2. **Submit command fills it**:
   ```python
   def submit(job_file, bundle=None, build=False):
       job = load_simjob(job_file)

       if build:
           bundle_ref = build_and_push()
       elif bundle == "latest":
           bundle_ref = find_latest()
       elif bundle:
           bundle_ref = bundle
       else:
           raise Error("Must specify --bundle or --build")

       job.bundle_ref = bundle_ref
       submit_to_k8s(job)
   ```

3. **Keep sampling pure**:
   ```python
   # Calabaria sampling knows nothing about bundles
   def sobol_sample(model, n):
       return SimJob(
           tasks=[...],  # No bundle_ref needed
           sampling_method="sobol"
       )
   ```

### Why This Is Better

1. **Clean separation** - Sampling is pure math, deployment is infrastructure
2. **Flexible binding** - Choose version at submission time
3. **Development friendly** - Can iterate without rebuilding bundles
4. **Preserves reproducibility** - Can always specify exact bundle

### The Philosophical Point

You're absolutely right that **sampling is about experimental design**, not code deployment. The tools should reflect this:

- `cb sampling` - Explores parameter space
- `cb bundle` - Manages code artifacts
- `mops jobs` - Executes experiments

Each tool has a clear, focused responsibility. The binding happens at the edges, not in the core.

This is Unix philosophy - small tools that compose well, not monolithic commands that do everything.

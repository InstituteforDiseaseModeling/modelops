# Artifact-Driven Job Validation User Guide

## Overview

ModelOps now validates job completion by checking that all expected outputs exist in ProvenanceStore, rather than blindly trusting Kubernetes exit codes. This ensures data integrity and enables recovery from partial failures without re-running successful tasks.

## Key Concepts

- **VALIDATING State**: Jobs transition here after Kubernetes completion to verify outputs
- **PARTIAL_SUCCESS**: New terminal state for jobs with some missing outputs
- **Output Manifest**: List of expected outputs generated when job is submitted
- **Resumable Jobs**: Partially completed jobs can be resumed with only missing tasks

## Command Reference

### 1. Submit a Job (Unchanged)

Submit jobs as before - the system now automatically generates an output manifest:

```bash
# Submit a simulation job
mops jobs submit examples/simulation_study.json

# Submit with specific bundle
mops jobs submit study.json --bundle sha256:abc123...
```

### 2. Sync with Validation

The `sync` command now validates outputs when jobs complete:

```bash
# Sync and validate outputs (default behavior)
mops jobs sync

# Sync without validation (trust K8s exit codes)
mops jobs sync --no-validate

# Dry run to see what would be updated
mops jobs sync --dry-run
```

**Example Output:**
```
🔄 Syncing 3 active jobs

  ↻ Validating outputs for job-abc123...
  ✓ job-abc123: All outputs verified (25 files)

  ↻ Validating outputs for job-def456...
  ⚠ job-def456: Partial success (18 verified, 7 missing)

  ✗ job-xyz789: Validation failed (0 outputs found)

✓ Updated 3 jobs
```

### 3. Manually Validate a Job

Check or re-check a job's outputs:

```bash
# Validate a specific job
mops jobs validate job-abc123

# Force re-validation even if already validated
mops jobs validate job-abc123 --force
```

**Example Output:**
```
🔍 Validating job job-abc123

Checking outputs in ProvenanceStore...

📊 Validation Results:
  Status: PARTIAL
  Verified: 18 outputs
  Missing: 7 outputs

❌ Missing outputs (first 5):
  • sims/abc123/shard/00/params_p1/seed_42
  • sims/abc123/shard/00/params_p1/seed_43
  • sims/abc123/shard/01/params_p2/seed_10
  • sims/abc123/shard/01/params_p2/seed_11
  • sims/abc123/shard/02/params_p3/seed_5
  ... and 2 more

📝 Updating job state based on validation...

✓ Job updated to partial

💡 This job can be resumed with:
  mops jobs resume job-abc123
```

### 4. Resume a Partial Job

Resume a job that completed with PARTIAL_SUCCESS status:

```bash
# Resume with automatic detection of missing tasks
mops jobs resume job-abc123

# Resume with different bundle version
mops jobs resume job-abc123 --bundle sha256:def456...

# Dry run to see what would be submitted
mops jobs resume job-abc123 --dry-run
```

**Example Output:**
```
♻️ Resuming job job-abc123
Found 7 tasks to resume

🚀 Submitting resume job...

✓ Resume job submitted: job-abc123-resume-20250112153045

Resuming 7 tasks from job job-abc123

💡 Track progress with:
  mops jobs status job-abc123-resume-20250112153045
```

### 5. Check Job Status

View detailed job information including validation status:

```bash
# Check specific job
mops jobs status job-abc123

# List all jobs
mops jobs list

# List only partial jobs
mops jobs list --status partial
```

**Example Status Output:**
```
📋 Job Details: job-abc123

Status:         PARTIAL_SUCCESS
Created:        2025-01-12 14:30:15
K8s Job:        job-abc123
Namespace:      modelops-dask-dev

Progress:
  Tasks Total:      25
  Tasks Completed:  25
  Tasks Verified:   18

Validation:
  Started:          2025-01-12 14:45:32
  Completed:        2025-01-12 14:45:35
  Missing Outputs:  7

💡 This job can be resumed with:
  mops jobs resume job-abc123
```

## Typical Workflows

### Workflow 1: Normal Job Completion

```bash
# 1. Submit job
mops jobs submit study.json
# Output: ✓ Job submitted: job-abc123

# 2. Monitor progress
mops jobs status job-abc123

# 3. Sync when K8s completes (automatic validation)
mops jobs sync
# Output: ✓ job-abc123: All outputs verified (25 files)
```

### Workflow 2: Handling Partial Failures

```bash
# 1. Job completes with missing outputs
mops jobs sync
# Output: ⚠ job-abc123: Partial success (18 verified, 7 missing)

# 2. Investigate what's missing
mops jobs validate job-abc123
# Shows list of missing outputs

# 3. Resume only the failed tasks
mops jobs resume job-abc123
# Output: ✓ Resume job submitted: job-abc123-resume-20250112153045

# 4. Monitor resume job
mops jobs status job-abc123-resume-20250112153045

# 5. Validate resume job completed
mops jobs sync
# Output: ✓ job-abc123-resume-20250112153045: All outputs verified (7 files)
```

### Workflow 3: Re-validation After Manual Fixes

```bash
# 1. Job shows as failed
mops jobs status job-abc123
# Status: FAILED

# 2. Manually fix issues (e.g., storage permissions)
# ...

# 3. Re-validate to check if outputs now exist
mops jobs validate job-abc123 --force
# Output: ✓ Job updated to succeeded

```

### Workflow 4: Bypass Validation for Quick Testing

```bash
# Sync without validation (old behavior)
mops jobs sync --no-validate
# Output: ✓ job-abc123: Marked as succeeded (no validation)
```

## Understanding Job States

The validation flow introduces new states:

```
PENDING → SUBMITTING → SCHEDULED → RUNNING → VALIDATING → SUCCEEDED
                                           ↓            ↘ PARTIAL_SUCCESS
                                           ↓            ↘ FAILED
                                      (K8s complete)   (based on outputs)
```

- **VALIDATING**: Checking if outputs exist in ProvenanceStore
- **PARTIAL_SUCCESS**: Some outputs missing (can resume)
- **SUCCEEDED**: All outputs verified present

## Troubleshooting

### Q: Why is my job stuck in VALIDATING?
A: Check if ProvenanceStore is accessible. Try manual validation:
```bash
mops jobs validate job-id --force
```

### Q: Can I resume a FAILED job?
A: No, only PARTIAL_SUCCESS jobs can be resumed. FAILED means infrastructure failure, not missing outputs.

### Q: How do I skip validation?
A: Use `--no-validate` flag with sync:
```bash
mops jobs sync --no-validate
```

### Q: What if validation is unavailable?
A: Jobs will proceed with traditional K8s-based completion. You'll see:
```
⚠ Validation unavailable: ProvenanceStore not configured
```

## Best Practices

1. **Always sync after jobs complete** to trigger validation
2. **Use resume for partial failures** instead of resubmitting entire jobs
3. **Check validation results** before assuming job success
4. **Use --dry-run** to preview actions before executing

## Environment Variables

```bash
# Set default environment
export MODELOPS_ENV=dev

# Skip validation by default (not recommended)
export MODELOPS_VALIDATE=false
```

## Performance Notes

- Validation typically completes in < 5 seconds for 100 outputs
- Resume jobs only submit missing tasks, saving compute time
- Validation results are cached in JobState for quick access

## Example Scripts

### Automated Resume on Partial Failure

```bash
#!/bin/bash
# auto_resume.sh - Automatically resume partial jobs

# Sync and check for partial jobs
mops jobs sync

# Get partial jobs
PARTIAL_JOBS=$(mops jobs list --status partial --json | jq -r '.[].job_id')

# Resume each partial job
for job_id in $PARTIAL_JOBS; do
    echo "Resuming $job_id..."
    mops jobs resume "$job_id"
done
```

### Validation Health Check

```bash
#!/bin/bash
# validation_check.sh - Check validation status of recent jobs

# Get jobs from last 24 hours
mops jobs list --since "24 hours ago" --json | jq -r '.[] | "\(.job_id): \(.status)"'

# Validate any unvalidated jobs
for job_id in $(mops jobs list --status succeeded --json | jq -r '.[].job_id'); do
    if [[ -z $(mops jobs status $job_id --json | jq -r '.validation_completed_at') ]]; then
        echo "Validating $job_id..."
        mops jobs validate "$job_id"
    fi
done
```
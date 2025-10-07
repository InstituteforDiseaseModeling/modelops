"""Cleanup CLI commands for managing Pulumi state issues."""

import os
import typer
import subprocess  # Still needed for TimeoutExpired exception
from pathlib import Path
from typing import Optional
from ..core import StackNaming, automation
from ..core.paths import ensure_work_dir, WORK_DIRS
from ..core.subprocess_utils import run_pulumi_command
from .utils import handle_pulumi_error, resolve_env
from .display import console, success, warning, error, info, section, dim, commands
from .common_options import env_option, yes_option

app = typer.Typer(help="Clean up Pulumi state and resources")


@app.command()
def unreachable(
    component: str = typer.Argument(
        ...,
        help="Component with unreachable resources (workspace, storage, adaptive)"
    ),
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Clean up unreachable Kubernetes resources from a stack.
    
    Use this when you get errors about unreachable clusters or
    deleted resources that Pulumi still tracks.
    
    Example:
        mops cleanup unreachable workspace
        mops cleanup unreachable storage --env prod
    """
    env = resolve_env(env)
    
    if component not in WORK_DIRS:
        error(f"Unknown component: {component}")
        info(f"Valid components: {', '.join(WORK_DIRS.keys())}")
        raise typer.Exit(1)
    
    stack_name = StackNaming.get_stack_name(component, env)
    work_dir = ensure_work_dir(component)
    
    if not yes:
        warning(f"\n⚠️  Cleaning unreachable resources from {component}")
        info(f"Stack: {stack_name}")
        info("This will remove unreachable Kubernetes resources from Pulumi state")
        info("Use this when the underlying cluster has been deleted")
        
        confirm = typer.confirm("\nProceed with cleanup?")
        if not confirm:
            success("Cleanup cancelled")
            raise typer.Exit(0)
    
    try:
        warning(f"\nCleaning up unreachable resources...")
        
        # Run refresh first to identify unreachable resources
        cmd = [
            "pulumi", "refresh",
            "--cwd", str(work_dir),
            "--stack", stack_name,
            "--yes"
        ]

        info("Refreshing stack to identify unreachable resources...")
        # Pass extra env var for unreachable resources
        result = run_pulumi_command(cmd, cwd=str(work_dir), env={"PULUMI_K8S_DELETE_UNREACHABLE": "true"})
        
        if "unreachable" in result.stdout or "unreachable" in result.stderr:
            # Now destroy the unreachable resources
            cmd = [
                "pulumi", "destroy",
                "--cwd", str(work_dir),
                "--stack", stack_name,
                "--yes"
            ]
            
            info("Removing unreachable resources from state...")
            result = run_pulumi_command(cmd, cwd=str(work_dir), env={"PULUMI_K8S_DELETE_UNREACHABLE": "true"})
            
            if result.returncode == 0:
                success(f"\n✓ Successfully cleaned up unreachable resources from {component}")
            else:
                error(f"Error during cleanup: {result.stderr}")
                raise typer.Exit(1)
        else:
            success(f"\n✓ No unreachable resources found in {component}")
        
    except Exception as e:
        error(f"Error during cleanup: {e}")
        raise typer.Exit(1)


@app.command()
def all(
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Clean up all stacks with errors or unreachable resources.
    
    Checks all ModelOps stacks and cleans up any that have
    unreachable resources or are in error state.
    
    Example:
        mops cleanup all
        mops cleanup all --env staging --yes
    """
    env = resolve_env(env)
    
    if not yes:
        warning("\n⚠️  Cleaning all stacks with errors")
        info("This will check and clean:")
        for component in WORK_DIRS.keys():
            info(f"  • {component}")
        
        confirm = typer.confirm("\nProceed with cleanup?")
        if not confirm:
            success("Cleanup cancelled")
            raise typer.Exit(0)
    
    section("\nChecking all stacks for issues...")
    
    cleaned = []
    skipped = []
    errors = []
    
    for component in WORK_DIRS.keys():
        stack_name = StackNaming.get_stack_name(component, env)
        work_dir = ensure_work_dir(component)
        
        info(f"\nChecking {component}...")
        
        try:
            # Check if stack exists
            cmd = ["pulumi", "stack", "--cwd", str(work_dir), "--stack", stack_name]
            result = run_pulumi_command(cmd, cwd=str(work_dir))
            
            if result.returncode != 0:
                dim(f"  Stack {stack_name} does not exist, skipping")
                skipped.append(component)
                continue
            
            # Try to refresh to check for issues
            cmd = ["pulumi", "refresh", "--cwd", str(work_dir), "--stack", stack_name, "--yes"]
            result = run_pulumi_command(cmd, cwd=str(work_dir), timeout=30)
            
            if "unreachable" in result.stdout or "unreachable" in result.stderr:
                warning(f"  Found unreachable resources in {component}")
                
                # Clean them up
                cmd = ["pulumi", "destroy", "--cwd", str(work_dir), "--stack", stack_name, "--yes"]
                result = run_pulumi_command(cmd, cwd=str(work_dir), env={"PULUMI_K8S_DELETE_UNREACHABLE": "true"})
                
                if result.returncode == 0:
                    success(f"  ✓ Cleaned up {component}")
                    cleaned.append(component)
                else:
                    error(f"  ✗ Failed to clean {component}")
                    errors.append(component)
            else:
                dim(f"  No issues found in {component}")
                skipped.append(component)
                
        except subprocess.TimeoutExpired:
            warning(f"  Timeout checking {component}, may need manual cleanup")
            errors.append(component)
        except Exception as e:
            error(f"  Error checking {component}: {e}")
            errors.append(component)
    
    # Summary
    section("\nCleanup Summary")
    if cleaned:
        success(f"Cleaned: {', '.join(cleaned)}")
    if skipped:
        info(f"No issues: {', '.join(skipped)}")
    if errors:
        error(f"Failed: {', '.join(errors)}")
        warning("\nFor failed components, try:")
        commands([
            ("Manual cleanup", f"mops cleanup unreachable <component>")
        ])


@app.command()
def orphaned(
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Remove orphaned resources (resources without parent stacks).
    
    Identifies and removes resources that exist in state but
    whose parent infrastructure has been deleted.
    
    Example:
        mops cleanup orphaned
        mops cleanup orphaned --env dev --yes
    """
    env = resolve_env(env)
    
    section("Checking for orphaned resources...")
    
    # Check if infra exists
    infra_work_dir = ensure_work_dir("infra")
    infra_stack = StackNaming.get_stack_name("infra", env)
    
    cmd = ["pulumi", "stack", "--cwd", str(infra_work_dir), "--stack", infra_stack]
    infra_result = run_pulumi_command(cmd, cwd=str(infra_work_dir), capture_output=True, text=True)
    
    infra_exists = infra_result.returncode == 0
    
    if not infra_exists:
        warning("\nInfrastructure stack does not exist")
        info("Checking for dependent stacks that may be orphaned...")
        
        orphaned = []
        for component in ["workspace", "storage", "adaptive"]:
            work_dir = ensure_work_dir(component)
            stack_name = StackNaming.get_stack_name(component, env)
            
            cmd = ["pulumi", "stack", "--cwd", str(work_dir), "--stack", stack_name]
            result = run_pulumi_command(cmd, cwd=str(work_dir), capture_output=True, text=True)

            if result.returncode == 0:
                orphaned.append(component)
        
        if orphaned:
            warning(f"\nFound orphaned stacks: {', '.join(orphaned)}")
            info("These stacks exist but their infrastructure parent is gone")
            
            if not yes:
                confirm = typer.confirm("\nDestroy orphaned stacks?")
                if not confirm:
                    success("Cleanup cancelled")
                    raise typer.Exit(0)
            
            for component in orphaned:
                warning(f"\nDestroying orphaned {component}...")
                
                # Use unreachable flag in case of K8s resources
                env_vars = os.environ.copy()
                env_vars["PULUMI_K8S_DELETE_UNREACHABLE"] = "true"
                
                work_dir = ensure_work_dir(component)
                stack_name = StackNaming.get_stack_name(component, env)
                
                cmd = [
                    "pulumi", "destroy",
                    "--cwd", str(work_dir),
                    "--stack", stack_name,
                    "--yes"
                ]

                result = run_pulumi_command(cmd, cwd=str(work_dir),
                                           env={"PULUMI_K8S_DELETE_UNREACHABLE": "true"},
                                           capture_output=True, text=True)
                
                if result.returncode == 0:
                    success(f"  ✓ Destroyed {component}")
                else:
                    error(f"  ✗ Failed to destroy {component}: {result.stderr}")
            
            success("\n✓ Orphaned resources cleaned up")
        else:
            success("\n✓ No orphaned resources found")
    else:
        success("\n✓ Infrastructure exists, no orphaned resources")


@app.command()
def reset(
    component: str = typer.Argument(
        ...,
        help="Component to reset (infra, workspace, storage, adaptive, or 'all')"
    ),
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Nuclear option: completely reset a stack.
    
    This destroys the stack and removes it from state entirely.
    Use with extreme caution!
    
    Example:
        mops cleanup reset workspace
        mops cleanup reset all --yes  # Reset everything
    """
    env = resolve_env(env)
    
    if component == "all":
        components = list(WORK_DIRS.keys())
    elif component in WORK_DIRS:
        components = [component]
    else:
        error(f"Unknown component: {component}")
        info(f"Valid: {', '.join(WORK_DIRS.keys())}, all")
        raise typer.Exit(1)
    
    if not yes:
        error("\n🔥 NUCLEAR RESET 🔥")
        warning("This will COMPLETELY DESTROY and remove:")
        for c in components:
            error(f"  • {c} stack and ALL resources")
        warning("\nThis action CANNOT be undone!")
        
        confirm_text = typer.prompt("\nType 'DESTROY' to confirm")
        if confirm_text != "DESTROY":
            success("Reset cancelled")
            raise typer.Exit(0)
    
    for comp in components:
        section(f"\nResetting {comp}...")
        
        work_dir = ensure_work_dir(comp)
        stack_name = StackNaming.get_stack_name(comp, env)
        
        # Force destroy with unreachable flag
        env_vars = os.environ.copy()
        env_vars["PULUMI_K8S_DELETE_UNREACHABLE"] = "true"
        
        # Destroy resources
        cmd = ["pulumi", "destroy", "--cwd", str(work_dir), "--stack", stack_name, "--yes"]
        run_pulumi_command(cmd, cwd=str(work_dir),
                          env={"PULUMI_K8S_DELETE_UNREACHABLE": "true"},
                          capture_output=True, text=True)
        
        # Remove the stack entirely
        cmd = ["pulumi", "stack", "rm", "--cwd", str(work_dir), "--stack", stack_name, "--yes"]
        result = run_pulumi_command(cmd, cwd=str(work_dir), capture_output=True, text=True)

        if result.returncode == 0:
            success(f"✓ Reset {comp} complete")
        else:
            warning(f"⚠ {comp} may already be removed")
    
    success("\n✓ Reset complete")
    info("You can now start fresh with 'mops infra up'")
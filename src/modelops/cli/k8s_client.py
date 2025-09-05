"""Kubernetes client helper for CLI commands.

This module provides utilities to get Kubernetes clients with fresh
kubeconfig from Pulumi state, avoiding stale local kubeconfig issues.
"""

import tempfile
import os
import subprocess
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path
from ..core import automation


def get_k8s_client(env: str) -> Tuple[Any, Any, str]:
    """Get Kubernetes client with current kubeconfig from Pulumi state.
    
    Args:
        env: Environment name
        
    Returns:
        Tuple of (CoreV1Api, AppsV1Api, temp_kubeconfig_path)
        
    Raises:
        ImportError: If kubernetes package is not installed
        Exception: If kubeconfig cannot be retrieved
    """
    try:
        from kubernetes import client, config
    except ImportError:
        raise ImportError(
            "kubernetes package not installed. "
            "Install with: pip install kubernetes"
        )
    
    # Get kubeconfig from infra stack
    try:
        outputs = automation.outputs("infra", env, refresh=False)
        if not outputs:
            raise Exception(f"No infrastructure found for environment: {env}")
        
        kubeconfig_yaml = automation.get_output_value(outputs, "kubeconfig")
        if not kubeconfig_yaml:
            raise Exception("No kubeconfig found in infrastructure outputs")
    except Exception as e:
        raise Exception(f"Failed to get kubeconfig from Pulumi: {e}")
    
    # Create temporary kubeconfig file
    # We don't use context manager here because we need the file to persist
    # The caller is responsible for cleanup using the returned path
    temp_file = tempfile.NamedTemporaryFile(
        mode='w', 
        suffix='.yaml', 
        prefix='kubeconfig-',
        delete=False
    )
    temp_file.write(kubeconfig_yaml)
    temp_file.flush()
    temp_file.close()
    
    try:
        # Load config from temp file
        config.load_kube_config(config_file=temp_file.name)
        
        # Create API clients
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        
        return v1, apps_v1, temp_file.name
    except Exception as e:
        # Clean up temp file on error
        os.unlink(temp_file.name)
        raise Exception(f"Failed to load kubeconfig: {e}")


def cleanup_temp_kubeconfig(temp_path: str) -> None:
    """Clean up temporary kubeconfig file.
    
    Args:
        temp_path: Path to temporary kubeconfig file
    """
    try:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    except Exception:
        # Ignore cleanup errors
        pass


def run_kubectl_with_fresh_config(
    cmd: List[str], 
    env: str,
    capture_output: bool = True,
    timeout: Optional[int] = 30
) -> subprocess.CompletedProcess:
    """Run kubectl command with fresh kubeconfig from Pulumi state.

    This is to prevent a mismatch between Pulumi's kubeconfig and the 
    kubconfig that kubectl uses by default (usually ~/.kube/config).
    
    This is for commands that don't have good Python API equivalents,
    like port-forward or interactive exec.
    
    Args:
        cmd: kubectl command arguments (without 'kubectl' prefix)
        env: Environment name
        capture_output: Whether to capture stdout/stderr
        timeout: Command timeout in seconds
        
    Returns:
        subprocess.CompletedProcess result
        
    Example:
        result = run_kubectl_with_fresh_config(
            ["get", "pods", "-n", "default"],
            env="dev"
        )
    """
    # Get kubeconfig from infra stack
    try:
        outputs = automation.outputs("infra", env, refresh=False)
        if not outputs:
            raise Exception(f"No infrastructure found for environment: {env}")
        
        kubeconfig_yaml = automation.get_output_value(outputs, "kubeconfig")
        if not kubeconfig_yaml:
            raise Exception("No kubeconfig found in infrastructure outputs")
    except Exception as e:
        raise Exception(f"Failed to get kubeconfig: {e}")
    
    # Create temporary kubeconfig file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml') as f:
        f.write(kubeconfig_yaml)
        f.flush()
        
        # Build full kubectl command
        full_cmd = ["kubectl", "--kubeconfig", f.name] + cmd
        
        # Run command
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=capture_output,
                text=True,
                timeout=timeout
            )
            return result
        except subprocess.TimeoutExpired as e:
            raise Exception(f"kubectl command timed out after {timeout}s: {' '.join(cmd)}")


def check_cluster_connectivity(env: str) -> Tuple[bool, str]:
    """Check if we can connect to the Kubernetes cluster.
    
    Args:
        env: Environment name
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        v1, _, temp_path = get_k8s_client(env)
        
        try:
            # Try to list namespaces as a connectivity check
            namespaces = v1.list_namespace(limit=1)
            cleanup_temp_kubeconfig(temp_path)
            return True, "Cluster connectivity verified"
        except Exception as e:
            cleanup_temp_kubeconfig(temp_path)
            return False, f"Cannot connect to cluster: {str(e)}"
    except Exception as e:
        return False, f"Failed to get cluster config: {str(e)}"


def get_pod_status(namespace: str, label_selector: str, env: str) -> List[Dict[str, Any]]:
    """Get status of pods matching label selector.
    
    Args:
        namespace: Kubernetes namespace
        label_selector: Label selector (e.g., "app=dask-scheduler")
        env: Environment name
        
    Returns:
        List of pod status dictionaries
    """
    v1, _, temp_path = get_k8s_client(env)
    
    try:
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector
        )
        
        result = []
        for pod in pods.items:
            result.append({
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "ready": all(
                    c.ready for c in (pod.status.container_statuses or [])
                ),
                "containers": len(pod.spec.containers),
                "node": pod.spec.node_name
            })
        
        return result
    finally:
        cleanup_temp_kubeconfig(temp_path)


def namespace_exists(namespace: str, env: str) -> bool:
    """Check if a namespace exists.
    
    Args:
        namespace: Namespace name to check
        env: Environment name
        
    Returns:
        True if namespace exists, False otherwise
    """
    v1, _, temp_path = get_k8s_client(env)
    
    try:
        v1.read_namespace(name=namespace)
        return True
    except:
        return False
    finally:
        cleanup_temp_kubeconfig(temp_path)

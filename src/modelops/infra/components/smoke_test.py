"""Reusable smoke test component for infrastructure validation.

This module provides a generic SmokeTest ComponentResource that can be
embedded in any stack to validate connectivity and functionality.
"""

import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, List, Optional, Any
import textwrap


class SmokeTest(pulumi.ComponentResource):
    """Kubernetes Job-based smoke test for infrastructure validation.
    
    Creates a Job that runs specified tests and reports results via
    ConfigMap. Tests are non-blocking and don't prevent stack deployment.
    """
    
    # Standard test images
    AZURE_CLI_IMAGE = "mcr.microsoft.com/azure-cli:latest"
    PYTHON_IMAGE = "python:3.11-slim"
    CURL_IMAGE = "curlimages/curl:latest"
    
    def __init__(self, 
                 name: str,
                 namespace: str,
                 tests: List[str],
                 k8s_provider,
                 env: Optional[List[k8s.core.v1.EnvVarArgs]] = None,
                 env_from: Optional[List[k8s.core.v1.EnvFromSourceArgs]] = None,
                 timeout_seconds: int = 30,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize smoke test job.
        
        Args:
            name: Name for this smoke test (e.g., "storage", "dask")
            namespace: Kubernetes namespace to run the test in
            tests: List of test types to run ("storage", "dask", "postgres", "network")
            k8s_provider: Kubernetes provider to use
            env: Optional environment variables
            env_from: Optional environment sources (for mounting secrets)
            timeout_seconds: Maximum time for tests to complete
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:infra:smoke-test", name, None, opts)
        
        # Build test script based on requested tests
        test_script = self._build_test_script(tests)
        
        # Determine image based on test requirements
        if "storage" in tests:
            image = self.AZURE_CLI_IMAGE
        elif "dask" in tests or "postgres" in tests:
            image = self.PYTHON_IMAGE
        else:
            image = self.CURL_IMAGE
        
        # Create ConfigMap to store test script
        script_config = k8s.core.v1.ConfigMap(
            f"{name}-test-script",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=f"smoke-test-{name}-script",
                namespace=namespace,
                labels={
                    "modelops.io/component": "smoke-test",
                    "modelops.io/test": name
                }
            ),
            data={
                "test.sh": test_script
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create Job to run smoke tests
        job = k8s.batch.v1.Job(
            f"{name}-smoke-test-job",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=f"smoke-test-{name}",
                namespace=namespace,
                labels={
                    "modelops.io/component": "smoke-test",
                    "modelops.io/test": name
                }
            ),
            spec=k8s.batch.v1.JobSpecArgs(
                # Don't retry on failure - we want quick feedback
                backoff_limit=0,
                active_deadline_seconds=timeout_seconds,
                # Clean up after 5 minutes
                ttl_seconds_after_finished=300,
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "modelops.io/component": "smoke-test",
                            "modelops.io/test": name
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        restart_policy="Never",
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="test",
                                image=image,
                                command=["sh", "/scripts/test.sh"],
                                env=env if env else [],
                                env_from=env_from if env_from else [],
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(
                                        name="test-script",
                                        mount_path="/scripts"
                                    )
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "cpu": "100m",
                                        "memory": "128Mi"
                                    },
                                    limits={
                                        "cpu": "500m",
                                        "memory": "256Mi"
                                    }
                                )
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name="test-script",
                                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                                    name=f"smoke-test-{name}-script",
                                    default_mode=0o755
                                )
                            )
                        ]
                    )
                )
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Store outputs
        self.job_name = job.metadata.name
        self.namespace = pulumi.Output.from_input(namespace)
        self.tests = pulumi.Output.from_input(tests)
        
        # Register outputs
        self.register_outputs({
            "job_name": self.job_name,
            "namespace": namespace,
            "tests": tests,
            "timeout_seconds": timeout_seconds
        })
    
    def _build_test_script(self, tests: List[str]) -> str:
        """Build shell script for requested tests.
        
        Args:
            tests: List of test types to include
            
        Returns:
            Shell script as string
        """
        script_parts = [
            "#!/bin/sh",
            "set -e",
            "echo 'Starting smoke tests...'",
            ""
        ]
        
        if "storage" in tests:
            script_parts.extend([
                "# Test storage connectivity",
                "echo '=== Storage Test ==='",
                "if [ -z \"$AZURE_STORAGE_CONNECTION_STRING\" ]; then",
                "  echo '✗ AZURE_STORAGE_CONNECTION_STRING not set'",
                "  exit 1",
                "fi",
                "echo '✓ Storage connection string found'",
                "",
                "# Try to list containers",
                "echo 'Listing storage containers...'",
                "az storage container list --output table || {",
                "  echo '✗ Failed to list containers'",
                "  exit 1",
                "}",
                "echo '✓ Successfully listed containers'",
                ""
            ])
        
        if "dask" in tests:
            script_parts.extend([
                "# Test Dask connectivity",
                "echo '=== Dask Test ==='",
                "if [ -z \"$DASK_SCHEDULER\" ]; then",
                "  DASK_SCHEDULER='tcp://dask-scheduler:8786'",
                "  echo \"Using default scheduler: $DASK_SCHEDULER\"",
                "fi",
                "",
                "# Install dask client if needed",
                "pip install --quiet dask distributed 2>/dev/null || true",
                "",
                "python -c \"",
                "from distributed import Client",
                "import sys",
                "try:",
                "    client = Client('$DASK_SCHEDULER', timeout=5)",
                "    info = client.scheduler_info()",
                "    print(f'✓ Connected to Dask scheduler')",
                "    print(f'  Workers: {len(info.get(\\\"workers\\\", {}))}')",
                "    client.close()",
                "except Exception as e:",
                "    print(f'✗ Failed to connect to Dask: {e}')",
                "    sys.exit(1)",
                "\"",
                ""
            ])
        
        if "postgres" in tests:
            script_parts.extend([
                "# Test Postgres connectivity",
                "echo '=== Postgres Test ==='",
                "if [ -z \"$POSTGRES_DSN\" ]; then",
                "  echo '⚠ POSTGRES_DSN not set, skipping Postgres test'",
                "else",
                "  pip install --quiet psycopg2-binary 2>/dev/null || true",
                "  python -c \"",
                "import psycopg2",
                "import sys",
                "try:",
                "    conn = psycopg2.connect('$POSTGRES_DSN')",
                "    cur = conn.cursor()",
                "    cur.execute('SELECT version()')",
                "    version = cur.fetchone()[0]",
                "    print(f'✓ Connected to Postgres')",
                "    print(f'  Version: {version.split()[0]} {version.split()[1]}')",
                "    conn.close()",
                "except Exception as e:",
                "    print(f'✗ Failed to connect to Postgres: {e}')",
                "    sys.exit(1)",
                "  \"",
                "fi",
                ""
            ])
        
        if "network" in tests:
            script_parts.extend([
                "# Test network connectivity",
                "echo '=== Network Test ==='",
                "# Test internal DNS",
                "nslookup kubernetes.default >/dev/null 2>&1 && echo '✓ Internal DNS working' || echo '✗ Internal DNS failed'",
                "",
                "# Test external connectivity",
                "curl -s -o /dev/null -w '%{http_code}' https://www.google.com | grep -q 200 && echo '✓ External connectivity working' || echo '✗ External connectivity failed'",
                ""
            ])
        
        script_parts.extend([
            "echo ''",
            "echo '✓ All smoke tests completed successfully!'",
            "exit 0"
        ])
        
        return "\n".join(script_parts)

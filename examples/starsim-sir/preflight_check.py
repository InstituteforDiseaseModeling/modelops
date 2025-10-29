#!/usr/bin/env python
"""
Pre-flight validation script to check model outputs before job submission.

This prevents runtime failures by validating:
1. Model has proper @model_output decorators
2. Outputs are actually extracted
3. Targets match model outputs
"""

import sys
import inspect
from pathlib import Path
import yaml


def validate_model(model_path, class_name):
    """Validate model has proper outputs."""
    print(f"\n{'='*60}")
    print(f"VALIDATING MODEL: {class_name}")
    print(f"{'='*60}")

    # Import the model
    import importlib.util
    spec = importlib.util.spec_from_file_location("model_module", model_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Get the model class
    model_class = getattr(module, class_name)
    model = model_class()

    # Check for extract methods
    extract_methods = []
    for name, method in inspect.getmembers(model):
        if name.startswith('extract_') and callable(method):
            # Skip extract_outputs - it's a special method
            if name == 'extract_outputs':
                continue
            extract_methods.append(name)
            # Check if it has the decorator
            if hasattr(method, '_output_name'):
                print(f"  ✓ {name} -> output: '{method._output_name}'")
            else:
                print(f"  ✗ {name} MISSING @model_output decorator!")
                return False

    if not extract_methods:
        print("  ✗ NO extract methods found!")
        return False

    # Test actual execution
    print(f"\nTesting execution:")
    try:
        from modelops_calabaria import ParameterSet
        params = ParameterSet(model.parameter_space(), {
            'beta': 0.08,
            'dur_inf': 5.0
        })
        state = model.build_sim(params, {})
        raw = model.run_sim(state, seed=42)

        print(f"  Raw output keys: {list(raw.keys())}")

        # Test extract methods
        for method_name in extract_methods:
            if method_name == 'extract_outputs':
                continue
            method = getattr(model, method_name)
            try:
                result = method(raw, 42)
                # Check for forbidden seed column
                if 'seed' in result.columns:
                    print(f"  ✗ {method_name} MUST NOT include 'seed' column (framework adds it)")
                    return False
                print(f"  ✓ {method_name} works, returns {type(result).__name__} with columns: {result.columns}")
            except Exception as e:
                print(f"  ✗ {method_name} FAILED: {e}")
                return False

    except Exception as e:
        print(f"  ✗ Execution failed: {e}")
        return False

    return True


def validate_registry():
    """Validate registry has outputs registered."""
    print(f"\n{'='*60}")
    print(f"VALIDATING REGISTRY")
    print(f"{'='*60}")

    registry_path = Path(".modelops-bundle/registry.yaml")
    if not registry_path.exists():
        print("  ✗ Registry not found!")
        return False

    with open(registry_path) as f:
        registry = yaml.safe_load(f)

    # Check models have outputs
    for model_name, model_info in registry.get('models', {}).items():
        outputs = model_info.get('outputs', [])
        if outputs:
            print(f"  ✓ Model {model_name} has outputs: {outputs}")
        else:
            print(f"  ✗ Model {model_name} has NO OUTPUTS!")
            return False

    # Check targets match outputs
    for target_name, target_info in registry.get('targets', {}).items():
        model_output = target_info.get('model_output')
        print(f"  Target {target_name} expects output: '{model_output}'")

        # Check if any model provides this output
        found = False
        for model_info in registry.get('models', {}).values():
            if model_output in model_info.get('outputs', []):
                found = True
                break

        if found:
            print(f"    ✓ Output '{model_output}' is provided by a model")
        else:
            print(f"    ✗ Output '{model_output}' NOT PROVIDED by any model!")
            return False

    return True


def main():
    """Run all validation checks."""
    print("PRE-FLIGHT VALIDATION")
    print("=" * 60)

    all_good = True

    # Validate model
    if not validate_model("models/sir.py", "StarsimSIR"):
        all_good = False

    # Validate registry
    if not validate_registry():
        all_good = False

    print(f"\n{'='*60}")
    if all_good:
        print("✅ ALL CHECKS PASSED - Safe to submit job")
        return 0
    else:
        print("❌ VALIDATION FAILED - Fix issues before submitting")
        return 1


if __name__ == "__main__":
    sys.exit(main())
# Python JSON UnboundLocalError Issue (LEGB Scoping)

## Issue Description

An `UnboundLocalError` occurred when the `json` module was used in our codebase:

```
UnboundLocalError: cannot access local variable 'json' where it is not associated with a value
```

Note: The error message text changed in Python 3.11+ from "local variable referenced before assignment" to "cannot access local variable 'X' where it is not associated with a value", but this is just a message change, not a behavior change.

## Root Cause - Python LEGB Scoping Rules

**This is NOT a Python 3.13-specific issue.** This is standard Python LEGB (Local, Enclosing, Global, Built-in) scoping behavior that has existed since Python's inception.

**The key insight:** Python determines whether a name is local or non-local **at compile time** by scanning the entire function body. If it finds ANY binding of that name ANYWHERE in the function, that name becomes local for the ENTIRE function - including lines that appear BEFORE the binding.

A name becomes local if it's bound ANYWHERE in the function via:
- Assignment: `json = something`
- Import statements: `import json` (inside the function)
- Exception clauses: `except json.JSONDecodeError as e` (attempts to access `json` as local)
- For loop targets: `for json in items`
- With statements: `with something as json`
- Function parameters: `def func(json)`
- Comprehensions (Python 3.x): `[json for json in items]`
- Walrus operator: `if (json := something):`

Once Python determines a name is local, ALL references to that name in the function are treated as local - even those that appear BEFORE the binding statement. This causes `UnboundLocalError` when you try to use the name before it's bound.

### The Actual Bugs Found

1. **In modelops_calabaria/modelops_wire.py:**
   ```python
   def wire_function(...):
       # Line 48 - tries to use json
       return {"metadata": json.dumps({...}).encode()}

       # ... many lines later ...

       # Line 108 - import makes json local!
       import json  # This makes ALL json references local
   ```
   The `import json` on line 108 made `json` a local variable, causing the earlier usage on line 48 to fail.

2. **In jsonrpc.py and subprocess_runner.py:**
   ```python
   import json  # Global import

   def some_function():
       try:
           data = json.loads(...)  # Tries to use global json
       except json.JSONDecodeError as e:  # This binds json as local!
           # Because Python sees json.JSONDecodeError, it tries to
           # access json as a local variable
   ```
   The `except json.JSONDecodeError` clause caused Python to treat `json` as a local variable.

## Example of Problematic Code

```python
import json  # Global import

def process_data():
    try:
        data = get_data()
        result = json.loads(data)  # This works fine
    except json.JSONDecodeError as e:  # This causes UnboundLocalError in Python 3.13
        handle_error(e)
```

## The Fix (Pick One)

### Option 1: Best Practice (Simple & Consistent)
Move imports to module scope and optionally import the exception for clarity:

```python
# At top of file
import json
from json import JSONDecodeError

def wire_function(...):
    # Use json normally
    payload = json.dumps(obj)

    try:
        data = json.loads(response)
    except JSONDecodeError as e:  # Clean, no local binding issues
        handle_error(e)
```

### Option 2: If You Need Local/Lazy Import
Put the import BEFORE any use in the function:

```python
def wire_function(...):
    import json  # MUST be before any json.* call in this function

    payload = json.dumps(obj)  # Now this works
    # ... rest of function
```

### Option 3: Aliased Local Import (What We Used)
Avoid shadowing by using an alias:

```python
def process_data():
    import json as json_module  # Binds json_module, not json

    data = json_module.loads(response)  # Use alias consistently
    # Can also catch ValueError since JSONDecodeError inherits from it
    try:
        result = json_module.loads(data)
    except ValueError as e:  # Avoids binding issues entirely
        handle_error(e)
```

## Files That Required Fixes

### ModelOps Core (Required)
1. **src/modelops/adapters/exec_env/isolated_warm.py** - ✅ REQUIRED
   - This processes results from subprocesses
   - Directly involved in the error path

2. **src/modelops/worker/subprocess_runner.py** - ✅ REQUIRED
   - Runs in isolated subprocess with Python 3.13
   - Communicates via JSON-RPC protocol
   - Executes the actual model code

3. **src/modelops/worker/jsonrpc.py** - ✅ REQUIRED
   - Handles JSON-RPC communication between processes
   - Used by process_manager.py to communicate with subprocesses

### ModelOps-Calabaria (Required)
4. **modelops-calabaria/src/modelops_calabaria/modelops_wire.py** - ✅ REQUIRED
   - The wire function that runs INSIDE the subprocess
   - This was the actual source of the error messages we saw
   - Gets installed into bundle venvs via pip

### Other Files with json imports (Not Required)
The following files have `import json` but weren't causing issues because they:
- Don't run in the subprocess execution path
- Don't use json in exception handlers
- Run in different Python contexts

- src/modelops/cli/dev.py
- src/modelops/core/state_manager.py
- src/modelops/services/storage/local.py
- (and others)

## Why All Subprocess-Related Files Needed Fixing

The execution flow is:

1. **Main process** (Python 3.13): `isolated_warm.py` prepares task
2. **Main process**: `process_manager.py` spawns subprocess
3. **Subprocess** (Python 3.13): `subprocess_runner.py` starts
4. **Subprocess**: Communicates via `jsonrpc.py` protocol
5. **Subprocess**: Loads bundle and calls `modelops_wire.py` (from Calabaria)
6. **Subprocess**: Returns results via JSON-RPC
7. **Main process**: `isolated_warm.py` processes results

Any `json` usage in this path with the problematic pattern would cause the error.

## The Venv Caching Issue

### Problem
ModelOps caches virtual environments to avoid recreating them for each task:

```
/tmp/modelops/venvs/
  └── sha256:be071-py3.13-e396d0cb/  # Cached venv for bundle
```

When we fixed the code, the cached venv still contained:
1. The OLD `subprocess_runner.py` (copied during venv creation)
2. The OLD `modelops-calabaria` package (installed from pip)

### Why This Is Fragile

1. **Subprocess runner gets copied**: When creating a venv, ModelOps copies
   `subprocess_runner.py` into it. If this file changes, existing venvs won't
   get the update.

2. **Package dependencies are frozen**: The venv installs packages once.
   Updates to modelops-calabaria won't be reflected unless the venv is
   recreated.

3. **Cache key doesn't include code version**: The cache key is based on:
   - Bundle digest (sha256:be071...)
   - Python version (py3.13)
   - A hash of requirements

   It doesn't include the ModelOps code version or subprocess_runner.py version.

### Recommendations

1. **Include subprocess_runner.py version in cache key**:
   ```python
   subprocess_runner_hash = hashlib.sha256(
       Path(subprocess_runner.__file__).read_bytes()
   ).hexdigest()[:8]
   cache_key = f"{bundle_digest}-py{python_version}-{deps_hash}-{subprocess_runner_hash}"
   ```

2. **Add force-refresh option**:
   ```python
   # In process_manager.py
   if os.environ.get('MODELOPS_FORCE_FRESH_VENV'):
       shutil.rmtree(venv_path, ignore_errors=True)
   ```

3. **Version subprocess_runner.py**:
   Add a version constant that gets checked:
   ```python
   # In subprocess_runner.py
   RUNNER_VERSION = "1.1.0"  # Increment when making breaking changes
   ```

4. **Consider not caching during development**:
   Add a development mode that always creates fresh venvs.

## Related Resources

### Python Documentation
- [What's New In Python 3.13](https://docs.python.org/3/whatsnew/3.13.html)
- [Built-in Exceptions - Python 3.13](https://docs.python.org/3/library/exceptions.html?highlight=unboundlocalerror)
- [Programming FAQ - Why do I get UnboundLocalError?](https://docs.python.org/3/faq/programming.html)

### Community Discussion
- [Real Python: Python 3.13 New Features](https://realpython.com/python313-new-features/)
- [Stack Overflow: UnboundLocalError in Python exception block](https://stackoverflow.com/questions/79682257/unboundlocalerror-in-python-exception-block)
- [GeeksforGeeks: UnboundLocalError Local variable Referenced Before Assignment](https://www.geeksforgeeks.org/python/unboundlocalerror-local-variable-referenced-before-assignment-in-python/)
- [Medium: Python's UnboundLocalError — Why it Occurs and How to Fix it](https://medium.com/@timothyjosephcw/pythons-unboundlocalerror-why-it-occurs-and-how-to-fix-it-71ea024365da)
- [Blog: Python's shadowing behavior always surprises me](https://ntietz.com/blog/pythons-shadowing-behavior-always-surprises-me/)

### Similar Issues in Other Projects
- [llama_index Issue #13133](https://github.com/run-llama/llama_index/issues/13133) - UnboundLocalError with json handling
- [mypy Issue #2400](https://github.com/python/mypy/issues/2400) - Not detecting UnboundLocalError

## Lessons Learned

1. **Understanding Python's LEGB scoping is critical** - This wasn't a Python 3.13 issue but a fundamental Python behavior we misunderstood initially. The LEGB rule means any binding of a name ANYWHERE in a function makes that name local throughout the entire function.

2. **Exception handlers can create surprising local bindings** - Writing `except json.JSONDecodeError` actually tries to bind `json` as a local variable, not just access the global module.

3. **Import placement matters in functions** - An `import json` statement inside a function makes `json` local to that function, affecting all references even those before the import.

4. **Subprocess isolation is complex** - Code runs in different Python interpreters with different module contexts.

5. **Caching strategies need versioning** - Any cached artifact should include version information for all its dependencies.

6. **Wire protocol code is critical** - The modelops_wire.py function from Calabaria runs in the hot path and needs careful testing.

7. **Local imports with aliasing are a safe workaround** - Using `import json as json_module` avoids the scoping confusion entirely.

## Finding Similar Issues

### Quick grep commands to find potential problems:

Look for function-local imports that could cause shadowing:
```bash
# Find function-local imports of json
rg -n --no-heading -C2 '^\s*(?:async\s+)?def\s+\w+\(.*\):' -g '!venv' \
  | rg -n --no-heading -C2 'import\s+json(\s|$)'

# Find other local bindings that would shadow a global module
rg -n --no-heading -C2 \
  '^\s*json\s*[:=]|for\s+json\s+in|with\s+.*\bas\s+json\b|except\s+.*\bas\s+json\b|\bjson\s*:=' \
  src/
```

## Testing Recommendations

1. **Test with target Python version**: Always test subprocess code with the exact Python version that will run in production.

2. **Clear caches during testing**: Add a test fixture that clears venv caches:
   ```python
   @pytest.fixture
   def clear_venv_cache():
       shutil.rmtree("/tmp/modelops/venvs", ignore_errors=True)
   ```

3. **Version subprocess components**: Add version checks to ensure compatibility.

4. **Monitor subprocess errors carefully**: They can be hard to debug since they run in isolation.

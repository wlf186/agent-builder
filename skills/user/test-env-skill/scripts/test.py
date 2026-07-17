import os
import sys
from importlib.metadata import distributions

print("Python executable:", sys.executable)
print("Python version:", sys.version)
print("Current working directory:", os.getcwd())

# Try to import PyPDF2 (installed in isolated env)
try:
    import PyPDF2
    print("PyPDF2 imported successfully, version:", PyPDF2.__version__)
except ImportError as e:
    print("PyPDF2 import failed:", e)

# List installed packages without relying on pip being installed in the managed
# uv environment.
packages = sorted(
    (distribution.metadata.get("Name", "unknown"), distribution.version)
    for distribution in distributions()
)
print("\nInstalled packages (first 10):")
for name, version in packages[:10]:
    print(f"  {name} {version}")

"""Allow `python -m tools.warehouse` to invoke the CLI."""
import sys
from tools.warehouse.cli import main
sys.exit(main())

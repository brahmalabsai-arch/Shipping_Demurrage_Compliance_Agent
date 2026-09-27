"""Seed every data domain built so far — the one documented seed command. Idempotent.

.venv\\Scripts\\python.exe -m scripts.seed
"""

from scripts import seed_port_rates, seed_shipments


def main() -> None:
    print("== shipments")
    seed_shipments.main()
    print("== demurrage port rates")
    seed_port_rates.main()


if __name__ == "__main__":
    main()

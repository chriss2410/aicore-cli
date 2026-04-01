"""aic — CLI entry point for AI Core deployment tooling."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="aic",
        description="AI Core — SAP AI Core deployment CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # aic setup
    setup_parser = subparsers.add_parser("setup", help="One-time AI Core infrastructure setup")
    setup_parser.add_argument("--auto", action="store_true", help="Non-interactive mode (skip prompts)")

    # aic deploy
    deploy_parser = subparsers.add_parser("deploy", help="Deploy a model to AI Core")
    deploy_parser.add_argument("--auto", action="store_true", help="Non-interactive mode (use deployment_config.yaml)")
    deploy_parser.add_argument("--config", default=None, help="Path to deployment_config.yaml (required with --auto)")
    deploy_parser.add_argument("--scenario", type=str, metavar="ID", help="Override scenario_id from config")
    deploy_parser.add_argument("--list", action="store_true", help="List all deployments")
    deploy_parser.add_argument("--list-artifacts", action="store_true", help="List all model artifacts")
    deploy_parser.add_argument("--list-configs", action="store_true", help="List all configurations")
    deploy_parser.add_argument("--status", type=str, metavar="ID", help="Check deployment status")
    deploy_parser.add_argument("--stop", type=str, metavar="ID", help="Stop a deployment")
    deploy_parser.add_argument("--logs", type=str, metavar="ID", help="Show deployment logs")
    deploy_parser.add_argument("--update", type=str, metavar="ID", help="Update deployment with new config")

    args = parser.parse_args()

    if args.command == "setup":
        from aic.setup import run
        run(auto=args.auto)

    elif args.command == "deploy":
        from aic import deploy

        if args.list:
            deploy.list_deployments()
        elif args.list_artifacts:
            deploy.list_artifacts()
        elif args.list_configs:
            deploy.list_configurations()
        elif args.status:
            deploy.check_status(args.status)
        elif args.stop:
            deploy.stop_deployment(args.stop)
        elif args.logs:
            deploy.query_logs(args.logs)
        elif args.update:
            deploy.update_deployment(args.update, config_path=args.config)
        else:
            deploy.run(auto=args.auto, config_path=args.config, scenario_id=args.scenario)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

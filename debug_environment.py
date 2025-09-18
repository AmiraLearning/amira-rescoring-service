#!/usr/bin/env python3

import os
import sys

# Add the project root to the Python path so we can import amira_pyutils
sys.path.insert(0, "/Users/nicholas/projects/amira-letter-scoring")

from amira_pyutils.environment import construct_envs, Environment, Environments


def debug_construct_envs() -> None:
    print("=== Debug Environment Loading ===\n")

    # Test the filename resolution
    default_file = "environments.template.yaml"
    override_env_var = "AMIRA_ENV_CONFIG_FILE"
    config_filename = os.environ.get(override_env_var, default_file)

    print(f"1. Config file resolution:")
    print(f"   Default file: {default_file}")
    print(f"   Override env var: {override_env_var}")
    print(f"   Override value: {os.environ.get(override_env_var, 'NOT SET')}")
    print(f"   Final config filename: {config_filename}")
    print(f"   File exists: {os.path.exists(config_filename)}")
    print()

    # Test construct_envs() directly
    print("2. Testing construct_envs():")
    try:
        envs = construct_envs()
        print(f"   Success! Found {len(envs)} environments:")
        for name, env in envs.items():
            print(f"     - {name}: {type(env)} (name='{env._name}')")
    except Exception as e:
        print(f"   Error: {e}")
        import traceback

        traceback.print_exc()
    print()

    # Test the Environments enum
    print("3. Testing Environments enum:")
    try:
        print(f"   Environments type: {type(Environments)}")
        print(f"   Available environments:")
        for env_item in Environments:
            print(f"     - {env_item.name}: {env_item.value._name}")
    except Exception as e:
        print(f"   Error: {e}")
        import traceback

        traceback.print_exc()
    print()

    # Test finding a specific environment
    print("4. Testing environment lookup:")
    try:
        staging_env = Environments.find("staging")
        if staging_env:
            print(f"   Found staging environment: {staging_env._name}")
            print(f"   SRS_url: {getattr(staging_env, '_SRS_url', 'NOT SET')}")
            try:
                # This should trigger the _check_not_empty validation
                srs_url = staging_env.SRS_url
                print(f"   SRS_url (via property): {srs_url}")
            except Exception as e:
                print(f"   Error accessing SRS_url: {e}")
        else:
            print("   Could not find staging environment")
    except Exception as e:
        print(f"   Error: {e}")
        import traceback

        traceback.print_exc()
    print()

    # Check some environment variables
    print("5. Environment variables check:")
    env_vars_to_check = [
        "SRS_URL",
        "STUDENT_RECORD_STORE_API",
        "SRS_KEY",
        "STUDENT_RECORD_STORE_KEY",
    ]
    for var in env_vars_to_check:
        value = os.getenv(var, "NOT SET")
        print(f"   {var}: {value}")
    print()


if __name__ == "__main__":
    debug_construct_envs()

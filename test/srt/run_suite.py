import argparse
import glob

from sglang.test.test_utils import run_unittest_files

suites = {
    "minimal": [
        "test_eval_accuracy.py",
        "test_embedding_openai_server.py",
        "test_openai_server.py",
        "test_vision_openai_server.py",
        "test_chunked_prefill.py",
        "test_torch_compile.py",
        "test_models_from_modelscope.py",
        "models/test_generation_models.py",
        "models/test_embedding_models.py",
        "sampling/penaltylib",
    ],
    "sampling/penaltylib": glob.glob(
        "sampling/penaltylib/**/test_*.py", recursive=True
    ),
}

for target_suite_name, target_tests in suites.items():
    for suite_name, tests in suites.items():
        if suite_name == target_suite_name:
            continue
        if target_suite_name in tests:
            tests.remove(target_suite_name)
            tests.extend(target_tests)

if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        "--timeout-per-file",
        type=int,
        default=2000,
        help="The time limit for running one file in seconds.",
    )
    arg_parser.add_argument(
        "--suite",
        type=str,
        default=list(suites.keys())[0],
        choices=list(suites.keys()) + ["all"],
        help="The suite to run",
    )
    args = arg_parser.parse_args()

    if args.suite == "all":
        files = glob.glob("**/test_*.py", recursive=True)
    else:
        files = suites[args.suite]

    exit_code = run_unittest_files(files, args.timeout_per_file)
    exit(exit_code)

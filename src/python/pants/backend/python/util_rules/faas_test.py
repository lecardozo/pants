# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Optional
from unittest.mock import Mock

import pytest

from pants.backend.awslambda.python.target_types import rules as target_type_rules
from pants.backend.python.target_types import (
    PythonRequirementTarget,
    PythonResolveField,
    PythonSourcesGeneratorTarget,
)
from pants.backend.python.target_types_rules import rules as python_target_types_rules
from pants.backend.python.util_rules.faas import (
    BuildPythonFaaSRequest,
    FaaSArchitecture,
    InferPythonFaaSHandlerDependency,
    PythonFaaSCompletePlatforms,
    PythonFaaSDependencies,
    PythonFaaSHandlerField,
    PythonFaaSHandlerInferenceFieldSet,
    PythonFaaSKnownRuntime,
    PythonFaaSLayoutField,
    PythonFaaSPex3VenvCreateExtraArgsField,
    PythonFaaSPexBuildExtraArgs,
    PythonFaaSRuntimeField,
    ResolvedPythonFaaSHandler,
    ResolvePythonFaaSHandlerRequest,
    RuntimePlatforms,
    RuntimePlatformsRequest,
    build_python_faas,
)
from pants.backend.python.util_rules.pex import CompletePlatforms, Pex
from pants.backend.python.util_rules.pex_from_targets import PexFromTargetsRequest
from pants.backend.python.util_rules.pex_venv import PexVenv, PexVenvLayout, PexVenvRequest
from pants.build_graph.address import Address
from pants.core.goals.package import OutputPathField
from pants.core.target_types import FileTarget
from pants.engine.fs import EMPTY_DIGEST, CreateDigest, Digest
from pants.engine.internals.scheduler import ExecutionError
from pants.engine.target import InferredDependencies, InvalidFieldException, Target
from pants.testutil.rule_runner import (
    MockGet,
    QueryRule,
    RuleRunner,
    engine_error,
    run_rule_with_mocks,
)
from pants.util.strutil import softwrap


class MockFaaS(Target):
    alias = "mock_faas"
    core_fields = (PythonFaaSDependencies, PythonFaaSHandlerField, PythonResolveField)


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *target_type_rules(),
            *python_target_types_rules(),
            QueryRule(ResolvedPythonFaaSHandler, [ResolvePythonFaaSHandlerRequest]),
            QueryRule(InferredDependencies, [InferPythonFaaSHandlerDependency]),
            QueryRule(RuntimePlatforms, [RuntimePlatformsRequest]),
        ],
        target_types=[
            FileTarget,
            MockFaaS,
            PythonRequirementTarget,
            PythonSourcesGeneratorTarget,
        ],
    )


@pytest.mark.parametrize("invalid_handler", ("path.to.lambda", "lambda.py"))
def test_handler_validation(invalid_handler: str) -> None:
    with pytest.raises(InvalidFieldException):
        PythonFaaSHandlerField(invalid_handler, Address("", target_name="t"))


def test_resolve_handler(rule_runner: RuleRunner) -> None:
    def assert_resolved(
        handler: str, *, expected_module: str, expected_func: str, is_file: bool
    ) -> None:
        addr = Address("src/python/project")
        rule_runner.write_files(
            {"src/python/project/lambda.py": "", "src/python/project/f2.py": ""}
        )
        field = PythonFaaSHandlerField(handler, addr)
        result = rule_runner.request(
            ResolvedPythonFaaSHandler, [ResolvePythonFaaSHandlerRequest(field)]
        )
        assert result.module == expected_module
        assert result.func == expected_func
        assert result.file_name_used == is_file

    assert_resolved(
        "path.to.lambda:func", expected_module="path.to.lambda", expected_func="func", is_file=False
    )
    assert_resolved(
        "lambda.py:func", expected_module="project.lambda", expected_func="func", is_file=True
    )

    with engine_error(contains="Unmatched glob"):
        assert_resolved(
            "doesnt_exist.py:func", expected_module="doesnt matter", expected_func="", is_file=True
        )
    # Resolving >1 file is an error.
    with engine_error(InvalidFieldException):
        assert_resolved(
            "*.py:func", expected_module="doesnt matter", expected_func="", is_file=True
        )


def test_infer_handler_dependency(rule_runner: RuleRunner, caplog) -> None:
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
                python_requirement(
                    name='ansicolors',
                    requirements=['ansicolors'],
                    modules=['colors'],
                )
                """
            ),
            "project/app.py": "",
            "project/ambiguous.py": "",
            "project/ambiguous_in_another_root.py": "",
            "project/BUILD": dedent(
                """\
                python_sources(sources=['app.py'])
                mock_faas(name='first_party', handler='project.app:func')
                mock_faas(name='first_party_shorthand', handler='app.py:func')
                mock_faas(name='third_party', handler='colors:func')
                mock_faas(name='unrecognized', handler='who_knows.module:func')

                python_sources(name="dep1", sources=["ambiguous.py"])
                python_sources(name="dep2", sources=["ambiguous.py"])
                mock_faas(
                    name="ambiguous",
                    handler='ambiguous.py:func',
                )
                mock_faas(
                    name="disambiguated",
                    handler='ambiguous.py:func',
                    dependencies=["!./ambiguous.py:dep2"],
                )

                python_sources(
                    name="ambiguous_in_another_root", sources=["ambiguous_in_another_root.py"]
                )
                mock_faas(
                    name="another_root__file_used",
                    handler="ambiguous_in_another_root.py:func",
                )
                mock_faas(
                    name="another_root__module_used",
                    handler="project.ambiguous_in_another_root:func",
                )
                """
            ),
            "src/py/project/ambiguous_in_another_root.py": "",
            "src/py/project/BUILD.py": "python_sources()",
        }
    )

    def assert_inferred(address: Address, *, expected: Optional[Address]) -> None:
        tgt = rule_runner.get_target(address)
        inferred = rule_runner.request(
            InferredDependencies,
            [InferPythonFaaSHandlerDependency(PythonFaaSHandlerInferenceFieldSet.create(tgt))],
        )
        assert inferred == InferredDependencies([expected] if expected else [])

    assert_inferred(
        Address("project", target_name="first_party"),
        expected=Address("project", relative_file_path="app.py"),
    )
    assert_inferred(
        Address("project", target_name="first_party_shorthand"),
        expected=Address("project", relative_file_path="app.py"),
    )
    assert_inferred(
        Address("project", target_name="third_party"),
        expected=Address("", target_name="ansicolors"),
    )
    assert_inferred(Address("project", target_name="unrecognized"), expected=None)

    # Warn if there's ambiguity, meaning we cannot infer.
    caplog.clear()
    assert_inferred(Address("project", target_name="ambiguous"), expected=None)
    assert len(caplog.records) == 1
    assert (
        softwrap(
            """
            project:ambiguous has the field `handler='ambiguous.py:func'`, which maps to the Python
            module `project.ambiguous`
            """
        )
        in caplog.text
    )
    assert "['project/ambiguous.py:dep1', 'project/ambiguous.py:dep2']" in caplog.text

    # Test that ignores can disambiguate an otherwise ambiguous handler. Ensure we don't log a
    # warning about ambiguity.
    caplog.clear()
    assert_inferred(
        Address("project", target_name="disambiguated"),
        expected=Address("project", target_name="dep1", relative_file_path="ambiguous.py"),
    )
    assert not caplog.records

    # Test that using a file path results in ignoring all targets which are not an ancestor. We can
    # do this because we know the file name must be in the current directory or subdir of the
    # `python_aws_lambda_function`.
    assert_inferred(
        Address("project", target_name="another_root__file_used"),
        expected=Address(
            "project",
            target_name="ambiguous_in_another_root",
            relative_file_path="ambiguous_in_another_root.py",
        ),
    )
    caplog.clear()
    assert_inferred(Address("project", target_name="another_root__module_used"), expected=None)
    assert len(caplog.records) == 1
    assert (
        softwrap(
            """
            ['project/ambiguous_in_another_root.py:ambiguous_in_another_root',
            'src/py/project/ambiguous_in_another_root.py']
            """
        )
        in caplog.text
    )

    # Test that we can turn off the inference.
    rule_runner.set_options(["--no-python-infer-entry-points"])
    assert_inferred(Address("project", target_name="first_party"), expected=None)


class TestRuntimeField(PythonFaaSRuntimeField):
    known_runtimes = (
        PythonFaaSKnownRuntime(
            "3.45", 3, 45, "", tag="faas-test-3-45", architecture=FaaSArchitecture.X86_64
        ),
        PythonFaaSKnownRuntime(
            "67.89", 67, 89, "", tag="faas-test-67-89", architecture=FaaSArchitecture.X86_64
        ),
    )

    def to_interpreter_version(self) -> None | tuple[int, int]:
        if self.value is None:
            return None

        first, second = self.value.split(".")
        return int(first), int(second)

    @classmethod
    def from_interpreter_version(cls, py_major: int, py_minor: int) -> str:
        return f"test:{py_major}.{py_minor}"


@pytest.mark.parametrize(
    ("value", "expected_interpreter_version", "expected_complete_platforms"),
    [
        pytest.param("3.45", (3, 45), ["complete_platform_faas-test-3-45.json"], id="known 3.45"),
        pytest.param(
            "67.89", (67, 89), ["complete_platform_faas-test-67-89.json"], id="known 67.89"
        ),
    ],
)
def test_infer_runtime_platforms_when_known_runtime_and_no_complete_platforms(
    value: str,
    expected_interpreter_version: tuple[int, int],
    expected_complete_platforms: list[str],
    rule_runner: RuleRunner,
) -> None:
    address = Address("path", target_name="target")

    request = RuntimePlatformsRequest(
        address=address,
        target_name="unused",
        runtime=TestRuntimeField(value, address),
        complete_platforms=PythonFaaSCompletePlatforms(None, address),
        architecture=FaaSArchitecture.X86_64,
    )

    platforms = rule_runner.request(RuntimePlatforms, [request])

    assert platforms == RuntimePlatforms(
        expected_interpreter_version,
        CompletePlatforms(expected_complete_platforms),
    )


def test_infer_runtime_platforms_errors_when_unknown_runtime_and_no_complete_platforms(
    rule_runner: RuleRunner,
) -> None:
    address = Address("path", target_name="target")

    request = RuntimePlatformsRequest(
        address=address,
        target_name="unused",
        runtime=TestRuntimeField("98.76", address),
        complete_platforms=PythonFaaSCompletePlatforms(None, address),
        architecture=FaaSArchitecture.X86_64,
    )

    with pytest.raises(
        ExecutionError,
        match=r"(?s).*Could not find a known runtime for the specified Python version",
    ):
        rule_runner.request(RuntimePlatforms, [request])


def test_infer_runtime_platforms_when_complete_platforms(
    rule_runner: RuleRunner,
) -> None:
    rule_runner.write_files({"path/BUILD": "file(name='cp', source='cp.json')", "path/cp.json": ""})
    address = Address("path", target_name="target")
    request = RuntimePlatformsRequest(
        address=address,
        target_name="unused",
        runtime=TestRuntimeField("completely ignored!", address),
        architecture=FaaSArchitecture.ARM64,  # ignored
        complete_platforms=PythonFaaSCompletePlatforms(["path:cp"], address),
    )

    platforms = rule_runner.request(RuntimePlatforms, [request])

    assert platforms == RuntimePlatforms(None, CompletePlatforms(["path/cp.json"]))


@pytest.mark.parametrize(
    ("ics", "expected_interpreter_version", "expected_complete_platforms"),
    [
        pytest.param(
            "==3.45.*",
            (3, 45),
            ["complete_platform_faas-test-3-45.json"],
            id="star",
        ),
        pytest.param(
            ">=3.45,<3.46", (3, 45), ["complete_platform_faas-test-3-45.json"], id="range"
        ),
    ],
)
def test_infer_runtime_platforms_when_known_narrow_ics_only(
    ics: str,
    expected_interpreter_version: tuple[int, int],
    expected_complete_platforms: list[str],
    rule_runner: RuleRunner,
) -> None:
    rule_runner.write_files(
        {
            "path/BUILD": f"python_sources(name='target', interpreter_constraints=['{ics}'])",
            "path/x.py": "",
        }
    )

    address = Address("path", target_name="target")
    request = RuntimePlatformsRequest(
        address=address,
        target_name="example_target",
        runtime=TestRuntimeField(None, address),
        complete_platforms=PythonFaaSCompletePlatforms(None, address),
        architecture=FaaSArchitecture.X86_64,
    )

    platforms = rule_runner.request(RuntimePlatforms, [request])

    assert platforms == RuntimePlatforms(
        expected_interpreter_version,
        CompletePlatforms(expected_complete_platforms),
    )


def test_infer_runtime_platforms_errors_when_unknown_narrow_ics(
    rule_runner: RuleRunner,
) -> None:
    rule_runner.write_files(
        {
            "path/BUILD": "python_sources(name='target', interpreter_constraints=['==3.33.*'])",
            "path/x.py": "",
        }
    )

    address = Address("path", target_name="target")
    request = RuntimePlatformsRequest(
        address=address,
        target_name="example_target",
        runtime=TestRuntimeField(None, address),
        complete_platforms=PythonFaaSCompletePlatforms(None, address),
        architecture=FaaSArchitecture.X86_64,
    )

    with pytest.raises(
        ExecutionError,
        match=r"(?s).*Could not find a known runtime for the inferred Python version",
    ):
        rule_runner.request(RuntimePlatforms, [request])


@pytest.mark.parametrize(
    "ics",
    [
        # specific patch might not be what the FaaS provider is using
        "==3.45.67",
        # wide version constraints are ambiguous
        ">=3.45",
        "<3.47,>=3.45",
    ],
)
def test_infer_runtime_platforms_errors_when_wide_ics(
    ics: str,
    rule_runner: RuleRunner,
) -> None:
    rule_runner.write_files(
        {
            "path/BUILD": f"python_sources(name='target', interpreter_constraints=['{ics}'])",
            "path/x.py": "",
        }
    )

    address = Address("path", target_name="target")
    request = RuntimePlatformsRequest(
        address=address,
        target_name="example_target",
        runtime=TestRuntimeField(None, address),
        architecture=FaaSArchitecture.X86_64,
        complete_platforms=PythonFaaSCompletePlatforms(None, address),
    )

    with pytest.raises(ExecutionError) as exc:
        rule_runner.request(RuntimePlatforms, [request])
    assert (
        "The 'example_target' target path:target cannot have its runtime platform inferred"
        in str(exc.value)
    )
    assert ics in str(exc.value)


def test_venv_create_extra_args_are_passed_through() -> None:
    # Setup
    addr = Address("addr")
    extra_args = (
        "--extra-args-for-test",
        "distinctive-value-FA943D37-51DA-445A-8F00-7E9C7DA8FAAA",
    )
    extra_args_field = PythonFaaSPex3VenvCreateExtraArgsField(extra_args, addr)
    request = BuildPythonFaaSRequest(
        address=addr,
        target_name="x",
        complete_platforms=Mock(),
        handler=None,
        output_path=OutputPathField(None, addr),
        runtime=Mock(),
        architecture=FaaSArchitecture.X86_64,
        pex3_venv_create_extra_args=extra_args_field,
        pex_build_extra_args=PythonFaaSPexBuildExtraArgs(None, addr),
        layout=PythonFaaSLayoutField(PexVenvLayout.FLAT_ZIPPED.value, addr),
        include_requirements=False,
        include_sources=False,
        reexported_handler_module=None,
    )

    observed_extra_args = []

    def mock_get_pex_venv(request: PexVenvRequest) -> PexVenv:
        observed_extra_args.append(request.extra_args)

        return PexVenv(digest=EMPTY_DIGEST, path=Path())

    # Exercise
    run_rule_with_mocks(
        build_python_faas,
        rule_args=[request],
        mock_gets=[
            MockGet(
                output_type=RuntimePlatforms,
                input_types=(RuntimePlatformsRequest,),
                mock=lambda _: RuntimePlatforms(interpreter_version=None),
            ),
            MockGet(
                output_type=ResolvedPythonFaaSHandler,
                input_types=(ResolvePythonFaaSHandlerRequest,),
                mock=lambda _: Mock(),
            ),
            MockGet(output_type=Digest, input_types=(CreateDigest,), mock=lambda _: EMPTY_DIGEST),
            MockGet(
                output_type=Pex,
                input_types=(PexFromTargetsRequest,),
                mock=lambda _: Pex(digest=EMPTY_DIGEST, name="pex", python=None),
            ),
            MockGet(output_type=PexVenv, input_types=(PexVenvRequest,), mock=mock_get_pex_venv),
        ],
    )

    # Verify
    assert len(observed_extra_args) == 1
    assert observed_extra_args[0] == extra_args


@pytest.mark.parametrize(
    "input_layout,expected_output",
    [
        (PexVenvLayout.FLAT_ZIPPED, "x.zip"),
        (PexVenvLayout.FLAT, "x"),
        # PexVenvLayout.VENV is semi-supported: if a user can get it to work, that's fine, but we don't explicitly support it.
    ],
)
def test_layout_should_be_passed_through_and_adjust_filename(input_layout, expected_output) -> None:
    # Setup
    addr = Address("x")
    request = BuildPythonFaaSRequest(
        address=addr,
        target_name="x",
        complete_platforms=Mock(),
        handler=None,
        output_path=OutputPathField(None, addr),
        runtime=Mock(),
        architecture=FaaSArchitecture.X86_64,
        pex3_venv_create_extra_args=Mock(),
        pex_build_extra_args=PythonFaaSPexBuildExtraArgs(None, addr),
        layout=input_layout,
        include_requirements=False,
        include_sources=False,
        reexported_handler_module=None,
    )

    mock_build = Mock()

    # Exercise
    run_rule_with_mocks(
        build_python_faas,
        rule_args=[request],
        mock_gets=[
            MockGet(
                output_type=RuntimePlatforms,
                input_types=(RuntimePlatformsRequest,),
                mock=lambda _: RuntimePlatforms(interpreter_version=None),
            ),
            MockGet(
                output_type=ResolvedPythonFaaSHandler,
                input_types=(ResolvePythonFaaSHandlerRequest,),
                mock=lambda _: Mock(),
            ),
            MockGet(output_type=Digest, input_types=(CreateDigest,), mock=lambda _: EMPTY_DIGEST),
            MockGet(
                output_type=Pex,
                input_types=(PexFromTargetsRequest,),
                mock=lambda _: Pex(digest=EMPTY_DIGEST, name="pex", python=None),
            ),
            MockGet(output_type=PexVenv, input_types=(PexVenvRequest,), mock=mock_build),
        ],
    )

    args = mock_build.mock_calls[0].args[0]
    assert args.layout == input_layout
    assert args.output_path.name == expected_output


def test_pex_build_extra_args_passed_through() -> None:
    addr = Address("addr")
    extra_args = ("--exclude=test_package",)

    extra_pex_args_field = PythonFaaSPexBuildExtraArgs(extra_args, addr)

    request = BuildPythonFaaSRequest(
        address=addr,
        target_name="test",
        complete_platforms=Mock(),
        handler=None,
        output_path=OutputPathField(None, addr),
        runtime=Mock(),
        architecture=FaaSArchitecture.X86_64,
        pex3_venv_create_extra_args=Mock(),
        pex_build_extra_args=extra_pex_args_field,
        layout=PythonFaaSLayoutField(PexVenvLayout.FLAT_ZIPPED.value, addr),
        include_requirements=False,
        include_sources=False,
        reexported_handler_module=None,
    )

    mock_build = Mock()

    # Exercise
    run_rule_with_mocks(
        build_python_faas,
        rule_args=[request],
        mock_gets=[
            MockGet(
                output_type=RuntimePlatforms,
                input_types=(RuntimePlatformsRequest,),
                mock=lambda _: RuntimePlatforms(interpreter_version=None),
            ),
            MockGet(
                output_type=ResolvedPythonFaaSHandler,
                input_types=(ResolvePythonFaaSHandlerRequest,),
                mock=lambda _: Mock(),
            ),
            MockGet(output_type=Digest, input_types=(CreateDigest,), mock=lambda _: EMPTY_DIGEST),
            MockGet(
                output_type=Pex,
                input_types=(PexFromTargetsRequest,),
                mock=mock_build,
            ),
            MockGet(output_type=PexVenv, input_types=(PexVenvRequest,), mock=Mock()),
        ],
    )

    assert extra_args[0] in mock_build.mock_calls[0].args[0].additional_args

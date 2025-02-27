import os
import re
import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import Callable
    from typing import Dict
    from typing import Iterable
    from typing import List
    from typing import Tuple
    from typing import Union

    from ddtrace import Span

import graphql
from graphql import MiddlewareManager
from graphql.error import GraphQLError
from graphql.execution import ExecutionResult
from graphql.language.source import Source

from ddtrace import config
from ddtrace.constants import ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_TYPE
from ddtrace.constants import SPAN_MEASURED_KEY
from ddtrace.internal.compat import stringify
from ddtrace.internal.utils import ArgumentError
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils import set_argument_value
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.version import parse_version
from ddtrace.internal.wrapping import unwrap
from ddtrace.internal.wrapping import wrap
from ddtrace.pin import Pin

from .. import trace_utils
from ...ext import SpanTypes


_graphql_version = parse_version(getattr(graphql, "__version__"))

if _graphql_version < (3, 0):
    from graphql.language.ast import Document
else:
    from graphql.language.ast import DocumentNode as Document


config._add(
    "graphql",
    dict(
        _default_service="graphql",
        resolvers_enabled=asbool(os.getenv("DD_TRACE_GRAPHQL_RESOLVERS_ENABLED", default=False)),
    ),
)

_GRAPHQL_SOURCE = "graphql.source"
_GRAPHQL_OPERATION_TYPE = "graphql.operation.type"
_GRAPHQL_OPERATION_NAME = "graphql.operation.name"


def patch():
    if getattr(graphql, "_datadog_patch", False):
        return
    setattr(graphql, "_datadog_patch", True)
    Pin().onto(graphql)

    for module_str, func_name, wrapper in _get_patching_candidates():
        _update_patching(wrap, module_str, func_name, wrapper)


def unpatch():
    if not getattr(graphql, "_datadog_patch", False) or _graphql_version < (2, 0):
        return

    for module_str, func_name, wrapper in _get_patching_candidates():
        _update_patching(unwrap, module_str, func_name, wrapper)

    setattr(graphql, "_datadog_patch", False)


def _get_patching_candidates():
    if _graphql_version < (3, 0):
        return [
            ("graphql.graphql", "execute_graphql", _traced_query),
            ("graphql.language.parser", "parse", _traced_parse),
            ("graphql.validation.validation", "validate", _traced_validate),
            ("graphql.execution.executor", "execute", _traced_execute),
        ]
    return [
        ("graphql.graphql", "graphql_impl", _traced_query),
        ("graphql.language.parser", "parse", _traced_parse),
        ("graphql.validation.validate", "validate", _traced_validate),
        ("graphql.execution.execute", "execute", _traced_execute),
    ]


def _update_patching(operation, module_str, func_name, wrapper):
    module = sys.modules[module_str]
    func = getattr(module, func_name)
    operation(func, wrapper)


def _traced_parse(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    source = get_argument_value(args, kwargs, 0, "source")
    source_str = _get_source_str(source)
    # If graphql.parse() is called outside graphql.graphql(), graphql.parse will
    # be a top level span. Therefore we must explicitly set the service name.
    with pin.tracer.trace(
        name="graphql.parse",
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        span.set_tag_str(_GRAPHQL_SOURCE, source_str)
        return func(*args, **kwargs)


def _traced_validate(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    document = get_argument_value(args, kwargs, 1, "ast")
    source_str = _get_source_str(document)
    # If graphql.validate() is called outside graphql.graphql(), graphql.validate will
    # be a top level span. Therefore we must explicitly set the service name.
    with pin.tracer.trace(
        name="graphql.validate",
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        span.set_tag_str(_GRAPHQL_SOURCE, source_str)
        errors = func(*args, **kwargs)
        _set_span_errors(errors, span)
        return errors


def _traced_execute(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    if config.graphql.resolvers_enabled:
        # patch resolvers
        args, kwargs = _inject_trace_middleware_to_args(_resolver_middleware, args, kwargs)

    # set resource name
    if _graphql_version < (3, 0):
        document = get_argument_value(args, kwargs, 1, "document_ast")
    else:
        document = get_argument_value(args, kwargs, 1, "document")
    source_str = _get_source_str(document)

    with pin.tracer.trace(
        name="graphql.execute",
        resource=source_str,
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        _set_span_operation_tags(span, document)
        span.set_tag_str(_GRAPHQL_SOURCE, source_str)

        result = func(*args, **kwargs)
        if isinstance(result, ExecutionResult):
            # set error tags if the result contains a list of GraphqlErrors, skip if it's a promise
            _set_span_errors(result.errors, span)
        return result


def _traced_query(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    # set resource name
    source = get_argument_value(args, kwargs, 1, "source")
    resource = _get_source_str(source)

    with pin.tracer.trace(
        name="graphql.request",
        resource=resource,
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        # mark span as measured and set sample rate
        span.set_tag(SPAN_MEASURED_KEY)
        sample_rate = config.graphql.get_analytics_sample_rate()
        if sample_rate is not None:
            span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, sample_rate)

        result = func(*args, **kwargs)
        if isinstance(result, ExecutionResult):
            # set error tags if the result contains a list of GraphqlErrors, skip if it's a promise
            # If the wrapped validate and execute functions return a list of errors we will duplicate
            # the span errors here.
            _set_span_errors(result.errors, span)
        return result


def _resolver_middleware(next_middleware, root, info, **args):
    """
    trace middleware which wraps the resolvers of graphql fields.
    Note - graphql middlewares can not be a partial. It must be a class or a function.
    """
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return next_middleware(root, info, **args)

    with pin.tracer.trace(
        name="graphql.resolve",
        resource=info.field_name,
        span_type=SpanTypes.GRAPHQL,
    ):
        return next_middleware(root, info, **args)


def _inject_trace_middleware_to_args(trace_middleware, args, kwargs):
    # type: (Callable, Tuple, Dict) -> Tuple[Tuple, Dict]
    """
    Adds a trace middleware to graphql.execute(..., middleware, ...)
    """
    middlewares_arg = 8
    if _graphql_version >= (3, 2):
        # middleware is the 10th argument graphql.execute(..) version 3.2+
        middlewares_arg = 9

    # get middlewares from args or kwargs
    try:
        middlewares = get_argument_value(args, kwargs, middlewares_arg, "middleware") or []
        if isinstance(middlewares, MiddlewareManager):
            # First we must get the middlewares iterable from the MiddlewareManager then append
            # trace_middleware. For the trace_middleware to be called a new MiddlewareManager will
            # need to initialized. This is handled in graphql.execute():
            # https://github.com/graphql-python/graphql-core/blob/v3.2.1/src/graphql/execution/execute.py#L254
            middlewares = middlewares.middlewares  # type: Iterable
    except ArgumentError:
        middlewares = []

    # Note - graphql middlewares are called in reverse order
    # add trace_middleware to the end of the list to wrap the execution of resolver and all middlewares
    middlewares = list(middlewares) + [trace_middleware]

    # update args and kwargs to contain trace_middleware
    args, kwargs = set_argument_value(args, kwargs, middlewares_arg, "middleware", middlewares)
    return args, kwargs


def _get_source_str(obj):
    # type: (Union[str, Source, Document]) -> str
    """
    Parses graphql Documents and Source objects to retrieve
    the graphql source input for a request.
    """
    if isinstance(obj, str):
        source_str = obj
    elif isinstance(obj, Source):
        source_str = obj.body
    elif isinstance(obj, Document):
        source_str = obj.loc.source.body
    else:
        source_str = ""
    # remove new lines, tabs and extra whitespace from source_str
    return re.sub(r"\s+", " ", source_str).strip()


def _set_span_errors(errors, span):
    # type: (List[GraphQLError], Span) -> None
    if not errors:
        # do nothing if the list of graphql errors is empty
        return

    span.error = 1
    exc_type_str = "%s.%s" % (GraphQLError.__module__, GraphQLError.__name__)
    span.set_tag_str(ERROR_TYPE, exc_type_str)
    error_msgs = "\n".join([stringify(error) for error in errors])
    # Since we do not support adding and visualizing multiple tracebacks to one span
    # we will not set the error.stack tag on graphql spans. Setting only one traceback
    # could be misleading and might obfuscate errors.
    span.set_tag_str(ERROR_MSG, error_msgs)


def _set_span_operation_tags(span, document):
    operation_def = graphql.get_operation_ast(document)
    if not operation_def:
        return

    # operation_def.operation should never be None
    if _graphql_version < (3, 0):
        span.set_tag_str(_GRAPHQL_OPERATION_TYPE, operation_def.operation)
    else:
        # OperationDefinition.operation is an Enum in graphql-core>=3
        span.set_tag_str(_GRAPHQL_OPERATION_TYPE, operation_def.operation.value)

    if operation_def.name:
        span.set_tag_str(_GRAPHQL_OPERATION_NAME, operation_def.name.value)

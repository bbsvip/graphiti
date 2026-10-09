# Full-suite failures observed locally

These are NOT a green release gate. Source test names and sanitized failure categories only.
No memo or credentials are included.

An ad-hoc combined Python verification runner also hit its 100-second core-suite
timeout under an altered child environment (OPENAI_API_KEY removed). Its cause is
not verified and that run is not counted as passing. Re-running the documented
CI command succeeded: 472 passed, 11 skipped in 13.41 seconds. No production fix
was inferred from the runner timeout.

## Resolved with owner approval

The three cases of
`server/tests/test_oauth_security.py::test_oauth_inference_preserves_schema_and_requires_completed_status`
(`[completed]`, `[incomplete]`, `[failed]`) now pass following the approved mock-only
update. The full non-integration server suite is 68 passed, 1 skipped, 1 deselected.
Bytes outside that test function were preserved; no production fake-only branch
was added.

## Remaining standalone MCP failures (last full run)

```text
FAILED mcp_server\tests\test_async_operations.py::TestAsyncQueueManagement::test_sequential_queue_processing - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncQueueManagement::test_concurrent_group_processing - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncQueueManagement::test_queue_overflow_handling - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestConcurrentOperations::test_concurrent_search_operations - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestConcurrentOperations::test_mixed_operation_concurrency - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncErrorHandling::test_timeout_recovery - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncErrorHandling::test_cancellation_handling - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncErrorHandling::test_exception_propagation - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncStreamHandling::test_large_response_streaming - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_async_operations.py::TestAsyncStreamHandling::test_incremental_processing - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_comprehensive_integration.py::TestCoreOperations::test_server_initialization - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestCoreOperations::test_add_text_memory - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestCoreOperations::test_add_json_memory - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestCoreOperations::test_add_message_memory - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestSearchOperations::test_search_nodes_semantic - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestSearchOperations::test_search_facts_with_filters - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestSearchOperations::test_hybrid_search - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestEpisodeManagement::test_get_episodes_pagination - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestEpisodeManagement::test_delete_episode - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestEntityAndEdgeOperations::test_get_entity_edge - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestErrorHandling::test_invalid_tool_arguments - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestErrorHandling::test_timeout_handling - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestErrorHandling::test_concurrent_operations - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestPerformance::test_latency_metrics - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_comprehensive_integration.py::TestPerformance::test_batch_processing_efficiency - mcp.shared.exceptions.MCPError: Connection closed
FAILED mcp_server\tests\test_stress_load.py::TestLoadScenarios::test_sustained_load - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestLoadScenarios::test_spike_load - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestLoadScenarios::test_memory_leak_detection - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestLoadScenarios::test_connection_pool_exhaustion - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestLoadScenarios::test_gradual_degradation - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestResourceLimits::test_large_payload_handling - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
FAILED mcp_server\tests\test_stress_load.py::TestResourceLimits::test_rate_limit_handling - pydantic_core._pydantic_core.ValidationError: 1 validation error for StdioS...
ERROR mcp_server\tests\test_async_operations.py::TestAsyncPerformance::test_async_throughput
ERROR mcp_server\tests\test_async_operations.py::TestAsyncPerformance::test_latency_under_load
```

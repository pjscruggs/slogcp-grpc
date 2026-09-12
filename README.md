# slogcp-grpc

Send `slogcp` logs through the Google Cloud Logging gRPC API while keeping your existing `log/slog` calls and enrichment.

This optional module uses Google's official [Cloud Logging client](https://pkg.go.dev/cloud.google.com/go/logging). It provides the client's buffering, batching, retries, and concurrent writes. Applications can tune CPU and memory use as their delivery needs grow. Throughput depends on payloads, batching, network conditions, and API quotas.

## Install

```sh
go get github.com/pjscruggs/slogcp-grpc
```

Check [go.mod](go.mod) for the required Go toolchain and `slogcp` dependency.

## Use

Create a `logging.Client` and configure its logger using the official options. Pass that logger to `NewExporter`, then pass the exporter to `slogcp.NewHandlerWithExporter`.

```go
client, err := logging.NewClient(ctx, projectID)
if err != nil {
    return err
}
client.OnError = func(err error) {
    fmt.Fprintln(os.Stderr, err)
}

cloudLogger := client.Logger("application",
    logging.EntryCountThreshold(1000),
    logging.ConcurrentWriteLimit(4),
    logging.BufferedByteLimit(32<<20),
)
exporter, err := slogcpgrpc.NewExporter(cloudLogger)
if err != nil {
    return errors.Join(err, client.Close())
}
handler, err := slogcp.NewHandlerWithExporter(exporter,
    slogcp.WithTraceProjectID(projectID),
)
if err != nil {
    return errors.Join(err, client.Close())
}
logger := slog.New(handler)
logger.InfoContext(ctx, "request complete", "status", 200)

// Stop logging producers before draining their queues.
return errors.Join(handler.Close(), exporter.Flush(), client.Close())
```

The [complete example](.examples/grpc/main.go) includes imports, bounded RPC deadlines, and cleanup. It uses Application Default Credentials and the `GOOGLE_CLOUD_PROJECT` environment variable. The caller needs permission to write logs in that project.

## Configuration

All options accepted by `logging.NewClient` remain available, including credentials, endpoint, connection pool, and transport settings. All `logging.LoggerOption` values remain available through `Client.Logger`.

| Setting | Official option |
| --- | --- |
| Flush interval | `logging.DelayThreshold` |
| Batch entry count | `logging.EntryCountThreshold` |
| Batch byte threshold | `logging.EntryByteThreshold` |
| Maximum entry batch bytes | `logging.EntryByteLimit` |
| Buffered bytes | `logging.BufferedByteLimit` |
| Concurrent writes | `logging.ConcurrentWriteLimit` |
| Background RPC context and deadline | `logging.ContextFunc` |
| Shared labels and resource | `logging.CommonLabels` and `logging.CommonResource` |
| Partial batch success | `logging.PartialSuccess` |
| Client source population | `logging.SourceLocationPopulation` |
| JSON redirection | `logging.RedirectAsJSON` |

Enable `slogcp.WithSourceLocationEnabled(true)` to capture the original logging call. The client's source population runs inside the exporter when slogcp has supplied no location.

`slogcp` preserves trace correlation, labels, severity, timestamps, source location, HTTP request metadata, and application payloads. String `logging.googleapis.com/insertId` attributes and operation groups are promoted into their API fields. Custom levels use the next lower named severity, with levels below Debug mapped to Debug and levels at or above Default mapped to Default. A zero timestamp follows the client's behavior and uses the current time.

`WithEntryMutator` accepts a callback that can set any writable `logging.Entry` field, including an insert ID, operation, per-entry monitored resource, or alternate payload. It receives the record context. Callbacks must support concurrent use. `Entry.LogName` follows the client's write restriction and must remain empty.

## Delivery and shutdown

The official client owns the delivery buffer. An additional `slogcp.WithAsync` queue is usually unnecessary. If you enable one, close the handler before flushing the exporter.

`Export` reports conversion and callback errors immediately. Buffered client validation, overflow, and delivery errors reach `Client.OnError` and the error returned by `Flush` or `Client.Close`. Set `OnError` before creating loggers. Its callback should return quickly. The client's flush summary can include errors from other loggers sharing that client.

`WithSynchronous()` makes each export wait for `logging.Logger.LogSync` using the record context. This bypasses batching. Direct calls to `Handler.Handle` receive delivery errors. Ordinary `slog.Logger` methods discard handler errors.

Stop producers, call `handler.Close`, then `exporter.Flush`, and finally `client.Close`. Check every error. Closing the handler leaves the borrowed logger and client open. `Flush` uses the supplied logger's RPC deadlines and leaves it available for further logs.

## Validation

```sh
go test -race ./...
go test -run '^$' -bench BenchmarkBufferedGRPC -benchmem ./...
```

Tests use a local gRPC service and need no cloud credentials. They cover delivery, shutdown, concurrent handler clones, metadata snapshots, API errors, cancellation, and buffer limits. The benchmark compares the official client, the exporter, and the slogcp handler against the same local service. It includes a flush every 1000 records. Cloud performance requires measurements in the intended runtime.

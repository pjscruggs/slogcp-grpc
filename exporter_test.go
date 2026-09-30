// Copyright 2025-2026 Patrick J. Scruggs
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package slogcpgrpc

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/logging"
	logpb "cloud.google.com/go/logging/apiv2/loggingpb"
	"github.com/pjscruggs/slogcp/v2"
	"google.golang.org/api/option"
	mrpb "google.golang.org/genproto/googleapis/api/monitoredres"
	logtypepb "google.golang.org/genproto/googleapis/logging/type"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

// recordingServer captures Cloud Logging write requests for assertions.
type recordingServer struct {
	logpb.UnimplementedLoggingServiceV2Server
	mu       sync.Mutex
	requests []*logpb.WriteLogEntriesRequest
	err      error
	discard  bool
}

// WriteLogEntries records a write request and returns a successful response.
func (s *recordingServer) WriteLogEntries(_ context.Context, request *logpb.WriteLogEntriesRequest) (*logpb.WriteLogEntriesResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.discard {
		s.requests = append(s.requests, proto.Clone(request).(*logpb.WriteLogEntriesRequest))
	}
	return &logpb.WriteLogEntriesResponse{}, s.err
}

// snapshot returns the write requests captured so far.
func (s *recordingServer) snapshot() []*logpb.WriteLogEntriesRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*logpb.WriteLogEntriesRequest(nil), s.requests...)
}

// newTestClient starts an in-process logging server and returns its client.
func newTestClient(t testing.TB, server *recordingServer) (*logging.Client, <-chan error) {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	rpcServer := grpc.NewServer()
	logpb.RegisterLoggingServiceV2Server(rpcServer, server)
	go func() { _ = rpcServer.Serve(listener) }()
	t.Cleanup(func() { rpcServer.Stop(); _ = listener.Close() })
	conn, err := grpc.NewClient("passthrough:///logging.test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client, err := logging.NewClient(context.Background(), "test-project", option.WithGRPCConn(conn))
	if err != nil {
		t.Fatal(err)
	}
	observedErrors := make(chan error, 100)
	client.OnError = func(err error) { observedErrors <- err }
	t.Cleanup(func() { _ = client.Close() })
	return client, observedErrors
}

// newTestLogger creates a Cloud Logging logger for a test client.
func newTestLogger(client *logging.Client, opts ...logging.LoggerOption) *logging.Logger {
	defaults := []logging.LoggerOption{
		logging.CommonResource(&mrpb.MonitoredResource{Type: "global", Labels: map[string]string{"project_id": "test-project"}}),
		logging.DelayThreshold(time.Hour),
		logging.ContextFunc(func() (context.Context, func()) {
			return context.WithTimeout(context.Background(), 5*time.Second)
		}),
	}
	return client.Logger("application", append(defaults, opts...)...)
}

// appEntries flattens application log entries from recorded requests.
func appEntries(requests []*logpb.WriteLogEntriesRequest) []*logpb.LogEntry {
	var result []*logpb.LogEntry
	for _, request := range requests {
		for _, entry := range request.Entries {
			if entry.GetJsonPayload().GetFields()["message"] != nil {
				result = append(result, entry)
			}
		}
	}
	return result
}

// TestBufferedMetadataAndOwnership checks buffered metadata conversion and entry ownership.
func TestBufferedMetadataAndOwnership(t *testing.T) {
	server := new(recordingServer)
	client, _ := newTestClient(t, server)
	operation := &logpb.LogEntryOperation{Id: "task-17", Producer: "worker", First: true, Last: true}
	resource := &mrpb.MonitoredResource{Type: "generic_task", Labels: map[string]string{"task_id": "17"}}
	mutationLabels := map[string]string{"release": "stable"}
	ctx := context.WithValue(context.Background(), contextKey{}, "insert-17")
	exporter, err := NewExporter(newTestLogger(client, logging.CommonLabels(map[string]string{"region": "example"}), logging.PartialSuccess()),
		WithEntryMutator(func(got context.Context, entry *logging.Entry) error {
			entry.InsertID = got.Value(contextKey{}).(string)
			entry.Operation, entry.Resource = operation, resource
			entry.Labels = mutationLabels
			return nil
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	when := time.Date(2026, 9, 1, 12, 0, 0, 123456789, time.UTC)
	payload := map[string]any{"message": "ready", "nested": map[string]any{"n": 7}, "raw": json.RawMessage(`{"ok":true}`)}
	source := slogcp.Entry{
		Payload: payload, Timestamp: when, Level: slogcp.LevelNotice.Level(),
		Trace: "projects/test-project/traces/0123456789abcdef0123456789abcdef", SpanID: "0123456789abcdef", TraceSampled: true,
		SourceLocation: &slogcp.SourceLocation{File: "worker.go", Line: 72, Function: "worker.run"},
		HTTPRequest: map[string]any{
			"requestMethod": "POST", "requestUrl": "https://example.com/jobs?q=1", "protocol": "HTTP/2",
			"userAgent": "test-agent", "referer": "https://example.com/", "requestSize": "123", "responseSize": "456",
			"status": 201, "latency": "0.123456789s", "remoteIp": "192.0.2.1", "serverIp": "192.0.2.2",
			"cacheHit": true, "cacheLookup": true, "cacheValidatedWithOriginServer": true, "cacheFillBytes": "789",
		},
	}
	if err := exporter.Export(ctx, source); err != nil {
		t.Fatal(err)
	}
	payload["nested"].(map[string]any)["n"] = 99
	source.SourceLocation.Line = 99
	operation.Id, resource.Labels["task_id"], mutationLabels["release"] = "changed", "changed", "changed"
	source.HTTPRequest["status"] = 500
	if err := exporter.Flush(); err != nil {
		t.Fatal(err)
	}
	requests := server.snapshot()
	entries := appEntries(requests)
	if len(entries) != 1 {
		t.Fatalf("got %d entries", len(entries))
	}
	entry := entries[0]
	if !entry.Timestamp.AsTime().Equal(when) || entry.Severity != logtypepb.LogSeverity_NOTICE || entry.Trace != source.Trace || entry.SpanId != source.SpanID || !entry.TraceSampled {
		t.Fatalf("correlation or severity changed %v", entry)
	}
	if entry.InsertId != "insert-17" || entry.Operation.Id != "task-17" || !entry.Operation.First || !entry.Operation.Last || entry.Resource.Labels["task_id"] != "17" || entry.Labels["release"] != "stable" || entry.SourceLocation.Line != 72 {
		t.Fatalf("metadata was not preserved %v", entry)
	}
	if got := entry.GetJsonPayload().AsMap()["nested"].(map[string]any)["n"]; got != float64(7) {
		t.Fatalf("borrowed payload changed %v", got)
	}
	if got := entry.GetJsonPayload().AsMap()["raw"].(map[string]any)["ok"]; got != true {
		t.Fatalf("custom JSON changed %v", got)
	}
	r := entry.HttpRequest
	if r.RequestMethod != "POST" || r.RequestUrl != "https://example.com/jobs?q=1" || r.Protocol != "HTTP/2" || r.UserAgent != "test-agent" || r.Referer != "https://example.com/" || r.RequestSize != 123 || r.ResponseSize != 456 || r.Status != 201 || r.Latency.AsDuration() != 123456789 || r.RemoteIp != "192.0.2.1" || r.ServerIp != "192.0.2.2" || !r.CacheHit || !r.CacheLookup || !r.CacheValidatedWithOriginServer || r.CacheFillBytes != 789 {
		t.Fatalf("HTTP metadata changed %v", r)
	}
	if requests[0].LogName != "projects/test-project/logs/application" || requests[0].Labels["region"] != "example" || !requests[0].PartialSuccess {
		t.Fatalf("logger options changed %v", requests[0])
	}
}

// contextKey identifies a test value carried through context.
type contextKey struct{}

// TestHandlerConcurrentClonesAndShutdown checks concurrent handler clones and shutdown.
func TestHandlerConcurrentClonesAndShutdown(t *testing.T) {
	server := new(recordingServer)
	client, _ := newTestClient(t, server)
	exporter, err := NewExporter(newTestLogger(client, logging.EntryCountThreshold(7), logging.ConcurrentWriteLimit(3)))
	if err != nil {
		t.Fatal(err)
	}
	handler, err := slogcp.NewHandlerWithExporter(exporter, slogcp.WithAsync(), slogcp.WithSourceLocationEnabled(true))
	if err != nil {
		t.Fatal(err)
	}
	logger := slog.New(handler)
	var wg sync.WaitGroup
	for worker := range 8 {
		wg.Go(func() {
			derived := logger.With("worker", worker).WithGroup("job")
			for job := range 20 {
				derived.Info("finished", "id", job)
			}
		})
	}
	wg.Wait()
	if err := handler.Close(); err != nil {
		t.Fatal(err)
	}
	if err := exporter.Flush(); err != nil {
		t.Fatal(err)
	}
	entries := appEntries(server.snapshot())
	if len(entries) != 160 {
		t.Fatalf("received %d of 160 entries", len(entries))
	}
	for _, entry := range entries {
		fields := entry.GetJsonPayload().AsMap()
		if fields["worker"] == nil || fields["job"].(map[string]any)["id"] == nil || entry.SourceLocation.GetFile() == "" {
			t.Fatalf("missing handler enrichment %v", entry)
		}
	}
	// Closing the handler leaves the borrowed exporter and client usable.
	if err := exporter.Export(context.Background(), slogcp.Entry{Payload: map[string]any{"message": "after drain"}}); err != nil {
		t.Fatal(err)
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	if got := len(appEntries(server.snapshot())); got != 161 {
		t.Fatalf("client close left %d entries", got)
	}
}

// TestBufferedDeliveryErrors checks delivery errors reported by buffered writes.
func TestBufferedDeliveryErrors(t *testing.T) {
	server := &recordingServer{err: status.Error(codes.PermissionDenied, "denied")}
	client, observedErrors := newTestClient(t, server)
	exporter, err := NewExporter(newTestLogger(client))
	if err != nil {
		t.Fatal(err)
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{Payload: map[string]any{"message": "denied"}}); err != nil {
		t.Fatal(err)
	}
	if err := exporter.Flush(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("flush error %v", err)
	}
	select {
	case err := <-observedErrors:
		if status.Code(err) != codes.PermissionDenied {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("OnError did not receive the delivery error")
	}
}

// TestClientCloseReportsDeliveryError checks errors reported when closing the client.
func TestClientCloseReportsDeliveryError(t *testing.T) {
	client, _ := newTestClient(t, &recordingServer{err: status.Error(codes.PermissionDenied, "denied")})
	exporter, err := NewExporter(newTestLogger(client))
	if err != nil {
		t.Fatal(err)
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{Payload: map[string]any{"message": "close"}}); err != nil {
		t.Fatal(err)
	}
	if err := client.Close(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("close error %v", err)
	}
}

// TestSynchronousDeliveryAndCancellation checks synchronous delivery and cancellation.
func TestSynchronousDeliveryAndCancellation(t *testing.T) {
	server := &recordingServer{err: status.Error(codes.PermissionDenied, "denied")}
	client, _ := newTestClient(t, server)
	exporter, err := NewExporter(newTestLogger(client), WithSynchronous())
	if err != nil {
		t.Fatal(err)
	}
	entry := slogcp.Entry{Payload: map[string]any{"message": "sync"}}
	if err := exporter.Export(context.Background(), entry); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("sync error %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := exporter.Export(ctx, entry); status.Code(err) != codes.Canceled {
		t.Fatalf("cancellation error %v", err)
	}
}

// TestConversionAndMutationErrors checks entry conversion and mutator failures.
func TestConversionAndMutationErrors(t *testing.T) {
	server := new(recordingServer)
	client, _ := newTestClient(t, server)
	exporter, err := NewExporter(newTestLogger(client))
	if err != nil {
		t.Fatal(err)
	}
	for name, entry := range map[string]slogcp.Entry{
		"payload":       {Payload: map[string]any{"bad": make(chan int)}},
		"http encoding": {HTTPRequest: map[string]any{"status": make(chan int)}},
		"http schema":   {HTTPRequest: map[string]any{"status": "invalid"}},
		"http url":      {HTTPRequest: map[string]any{"requestUrl": "http://%"}},
	} {
		t.Run(name, func(t *testing.T) {
			if err := exporter.Export(context.Background(), entry); err == nil {
				t.Fatal("expected conversion error")
			}
		})
	}
	want := errors.New("mutation failed")
	exporter, err = NewExporter(newTestLogger(client), WithEntryMutator(func(context.Context, *logging.Entry) error { return want }))
	if err != nil {
		t.Fatal(err)
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{}); !errors.Is(err, want) {
		t.Fatalf("mutation error %v", err)
	}
	if err := exporter.Flush(); err != nil {
		t.Fatal(err)
	}
	if len(server.snapshot()) != 0 {
		t.Fatal("invalid entries reached the service")
	}
}

// TestBufferedLimitErrors checks errors when buffered writes exceed limits.
func TestBufferedLimitErrors(t *testing.T) {
	client, observedErrors := newTestClient(t, new(recordingServer))
	exporter, err := NewExporter(newTestLogger(client, logging.EntryByteLimit(1)))
	if err != nil {
		t.Fatal(err)
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{Payload: map[string]any{"message": "oversized"}}); err != nil {
		t.Fatal(err)
	}
	if err := exporter.Flush(); !errors.Is(err, logging.ErrOversizedEntry) {
		t.Fatalf("limit error %v", err)
	}
	select {
	case err := <-observedErrors:
		if !errors.Is(err, logging.ErrOversizedEntry) {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("OnError did not receive oversized entry")
	}
}

// TestMutatorProtoOwnershipAndOrder checks mutator ordering and protobuf ownership.
func TestMutatorProtoOwnershipAndOrder(t *testing.T) {
	server := new(recordingServer)
	client, _ := newTestClient(t, server)
	var payload *structpb.Struct
	payload, _ = structpb.NewStruct(map[string]any{"message": "proto"})
	exporter, err := NewExporter(newTestLogger(client),
		WithEntryMutator(func(_ context.Context, entry *logging.Entry) error {
			entry.Payload = payload
			entry.InsertID = "first"
			return nil
		}),
		WithEntryMutator(func(_ context.Context, entry *logging.Entry) error {
			if entry.InsertID != "first" {
				t.Fatal("mutator order changed")
			}
			return nil
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{}); err != nil {
		t.Fatal(err)
	}
	payload.Fields["message"] = structpb.NewStringValue("changed")
	if err := exporter.Flush(); err != nil {
		t.Fatal(err)
	}
	entries := appEntries(server.snapshot())
	if len(entries) != 1 || entries[0].GetJsonPayload().GetFields()["message"].GetStringValue() != "proto" {
		t.Fatalf("mutator payload changed %v", entries)
	}
}

// TestSpecialPayloadFields checks conversion of special payload fields.
func TestSpecialPayloadFields(t *testing.T) {
	server := new(recordingServer)
	client, _ := newTestClient(t, server)
	exporter, err := NewExporter(newTestLogger(client))
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"message": "operation", "logging.googleapis.com/insertId": "id-1",
		"logging.googleapis.com/operation": map[string]any{"id": "op-1", "producer": "worker", "first": true, "last": true},
	}
	if err := exporter.Export(context.Background(), slogcp.Entry{Payload: payload}); err != nil {
		t.Fatal(err)
	}
	if payload["logging.googleapis.com/insertId"] != "id-1" || payload["logging.googleapis.com/operation"] == nil {
		t.Fatal("borrowed payload was modified")
	}
	if err := exporter.Flush(); err != nil {
		t.Fatal(err)
	}
	entries := appEntries(server.snapshot())
	if len(entries) != 1 {
		t.Fatalf("got %d entries", len(entries))
	}
	entry := entries[0]
	if entry.InsertId != "id-1" || entry.Operation.Id != "op-1" || entry.Operation.Producer != "worker" || !entry.Operation.First || !entry.Operation.Last {
		t.Fatalf("special fields were not promoted %v", entry)
	}
	if len(entry.GetJsonPayload().Fields) != 1 {
		t.Fatalf("special fields stayed in payload %v", entry.GetJsonPayload())
	}
}

// TestConstructionAndSeverity checks exporter construction and severity mapping.
func TestConstructionAndSeverity(t *testing.T) {
	if _, err := NewExporter(nil); err == nil {
		t.Fatal("nil logger accepted")
	}
	var nilExporter *Exporter
	if err := nilExporter.Export(context.Background(), slogcp.Entry{}); err == nil {
		t.Fatal("nil exporter accepted")
	}
	if err := nilExporter.Flush(); err == nil {
		t.Fatal("nil exporter flushed")
	}
	for _, tc := range []struct {
		level slog.Level
		want  logging.Severity
	}{
		{-100, logging.Debug}, {-4, logging.Debug}, {-1, logging.Debug}, {0, logging.Info}, {1, logging.Info},
		{2, logging.Notice}, {3, logging.Notice}, {4, logging.Warning}, {7, logging.Warning}, {8, logging.Error},
		{11, logging.Error}, {12, logging.Critical}, {15, logging.Critical}, {16, logging.Alert}, {19, logging.Alert},
		{20, logging.Emergency}, {29, logging.Emergency}, {30, logging.Default}, {31, logging.Default},
	} {
		t.Run(fmt.Sprint(tc.level), func(t *testing.T) {
			if got := severity(tc.level); got != tc.want {
				t.Fatalf("severity %v want %v", got, tc.want)
			}
		})
	}
}

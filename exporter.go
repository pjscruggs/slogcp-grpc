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

// Package slogcpgrpc sends enriched slogcp entries through the Google Cloud
// Logging client and its gRPC transport.
//
// Configure authentication and transport with logging.NewClient and batching
// with Client.Logger. NewExporter borrows that logger and preserves its options.
// Stop producers and finish draining or aborting the slogcp handler before
// flushing the exporter and closing the client. A handler shutdown timeout can
// leave workers running. The logging client owns the delivery buffers.
package slogcpgrpc

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"net/url"

	"cloud.google.com/go/logging"
	logpb "cloud.google.com/go/logging/apiv2/loggingpb"
	"github.com/pjscruggs/slogcp/v2"
	mrpb "google.golang.org/genproto/googleapis/api/monitoredres"
	logtypepb "google.golang.org/genproto/googleapis/logging/type"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/structpb"
)

// Exporter adapts a Google Cloud Logging logger to slogcp.EntryExporter.
// Export and Flush may be called concurrently. Its zero value is unusable.
// The caller owns the logging client and must stop exports before closing it.
type Exporter struct {
	logger      *logging.Logger
	synchronous bool
	mutators    []func(context.Context, *logging.Entry) error
}

var _ slogcp.EntryExporter = (*Exporter)(nil)

// Option configures an Exporter during construction.
type Option func(*Exporter)

// WithSynchronous waits for logging.Logger.LogSync in each Export call and
// returns delivery errors. The record context controls the RPC deadline.
// Handler.Handle returns those errors when no async handler wraps the exporter.
// With slogcp.WithAsync, Handle returns after enqueue and worker errors go to
// the async error writer. Ordinary slog.Logger methods discard handler errors.
func WithSynchronous() Option {
	return func(e *Exporter) { e.synchronous = true }
}

// WithEntryMutator updates a converted entry before delivery. Multiple mutators
// run in option order. A returned error prevents delivery and reaches Export.
//
// The callback may set InsertID, Operation, Resource, or any other writable
// logging.Entry field. Payload initially contains an owned json.RawMessage.
// The callback may change it or replace it with any payload the client supports.
// Reference data supplied by the callback is consumed or copied before Export
// returns. The callback must be safe for concurrent calls and must not retain
// the entry or mutate its data concurrently. Entry.LogName must remain empty.
func WithEntryMutator(f func(context.Context, *logging.Entry) error) Option {
	return func(e *Exporter) {
		if f != nil {
			e.mutators = append(e.mutators, f)
		}
	}
}

// NewExporter borrows logger without changing its configuration or ownership.
// All logging.Client and logging.Logger options remain available directly.
// Buffered delivery is the default and uses logging.Logger.Log.
func NewExporter(logger *logging.Logger, opts ...Option) (*Exporter, error) {
	if logger == nil {
		return nil, errors.New("slogcpgrpc received a nil logging logger")
	}
	e := &Exporter{logger: logger}
	for _, opt := range opts {
		if opt != nil {
			opt(e)
		}
	}
	return e, nil
}

// Export converts an enriched entry and submits it to the logging client.
// Conversion and mutator errors are returned immediately. In buffered mode,
// client validation, overflow, and delivery errors reach logging.Client.OnError
// and Flush or logging.Client.Close. A nil return means the client was called.
// It does not guarantee that the entry was buffered or delivered.
//
// The entry context reaches mutators and synchronous RPCs. Buffered RPCs use
// logging.ContextFunc from the supplied logger. Borrowed entry data is fully
// consumed or copied before Export returns.
func (e *Exporter) Export(ctx context.Context, source slogcp.Entry) error {
	if e == nil || e.logger == nil {
		return errors.New("slogcpgrpc exporter is uninitialized")
	}
	entry, err := convertEntry(source)
	if err != nil {
		return err
	}
	for _, mutate := range e.mutators {
		if err := mutate(ctx, &entry); err != nil {
			return err
		}
	}
	// The official client retains these maps and protobuf pointers while its
	// bundler works. Detach any references installed by a mutator.
	if len(e.mutators) > 0 {
		snapshotMetadata(&entry)
	}
	if e.synchronous {
		if err := e.logger.LogSync(ctx, entry); err != nil {
			return fmt.Errorf("slogcpgrpc synchronous delivery failed %w", err)
		}
		return nil
	}
	e.logger.Log(entry)
	return nil
}

// Flush waits for currently buffered entries. It returns the client's error
// summary since the previous flush, including errors from other loggers that
// share the client. Set logging.Client.OnError before creating loggers for
// individual asynchronous errors. Flush has the supplied logger's RPC deadlines.
// It does not close the client or prevent further exports.
func (e *Exporter) Flush() error {
	if e == nil || e.logger == nil {
		return errors.New("slogcpgrpc exporter is uninitialized")
	}
	if err := e.logger.Flush(); err != nil {
		return fmt.Errorf("slogcpgrpc flush failed %w", err)
	}
	return nil
}
// convertEntry converts source to a Cloud Logging entry, promotes supported
// reserved payload fields, and snapshots borrowed source data for safe buffered
// delivery. It returns an error when payload or structured metadata conversion
// fails.
func convertEntry(source slogcp.Entry) (logging.Entry, error) {
	entry := logging.Entry{}
	payloadFields := source.Payload
	if insertID, ok := payloadFields["logging.googleapis.com/insertId"].(string); ok {
		entry.InsertID = insertID
		payloadFields = maps.Clone(payloadFields)
		delete(payloadFields, "logging.googleapis.com/insertId")
	}
	if operation, ok := payloadFields["logging.googleapis.com/operation"].(map[string]any); ok {
		data, err := json.Marshal(operation)
		if err != nil {
			return logging.Entry{}, fmt.Errorf("slogcpgrpc cannot encode operation %w", err)
		}
		entry.Operation = new(logpb.LogEntryOperation)
		if err := protojson.Unmarshal(data, entry.Operation); err != nil {
			return logging.Entry{}, fmt.Errorf("slogcpgrpc cannot decode operation %w", err)
		}
		payloadFields = maps.Clone(payloadFields)
		delete(payloadFields, "logging.googleapis.com/operation")
	}
	// RawMessage takes an owned snapshot without making the logging client
	// repeat JSON marshaling. Custom marshalers keep their ordinary JSON rules.
	payload, err := json.Marshal(payloadFields)
	if err != nil {
		return logging.Entry{}, fmt.Errorf("slogcpgrpc cannot encode payload %w", err)
	}
	entry.Payload = json.RawMessage(payload)
	entry.Timestamp = source.Timestamp
	entry.Severity = severity(source.Level)
	entry.Trace = source.Trace
	entry.SpanID = source.SpanID
	entry.TraceSampled = source.TraceSampled
	entry.Labels = maps.Clone(source.Labels)
	if location := source.SourceLocation; location != nil {
		entry.SourceLocation = &logpb.LogEntrySourceLocation{
			File: location.File, Line: location.Line, Function: location.Function,
		}
	}
	if source.HTTPRequest != nil {
		entry.HTTPRequest, err = convertHTTPRequest(source.HTTPRequest)
		if err != nil {
			return logging.Entry{}, err
		}
	}
	return entry, nil
}

// snapshotMetadata copies mutable entry metadata and supported protobuf
// payloads so the logging client does not retain mutator-owned references.
func snapshotMetadata(entry *logging.Entry) {
	entry.Labels = maps.Clone(entry.Labels)
	if entry.Operation != nil {
		entry.Operation = proto.Clone(entry.Operation).(*logpb.LogEntryOperation)
	}
	if entry.Resource != nil {
		entry.Resource = proto.Clone(entry.Resource).(*mrpb.MonitoredResource)
	}
	if entry.SourceLocation != nil {
		entry.SourceLocation = proto.Clone(entry.SourceLocation).(*logpb.LogEntrySourceLocation)
	}
	switch p := entry.Payload.(type) {
	case *structpb.Struct:
		if p != nil {
			entry.Payload = proto.Clone(p).(*structpb.Struct)
		}
	case *anypb.Any:
		if p != nil {
			entry.Payload = proto.Clone(p).(*anypb.Any)
		}
	}
}

// convertHTTPRequest converts normalized Cloud Logging HTTP request fields to
// the logging client's HTTPRequest representation. It returns an error if the
// fields cannot be encoded, decoded according to the Cloud Logging schema, or
// parsed as a request URL.
func convertHTTPRequest(fields map[string]any) (*logging.HTTPRequest, error) {
	data, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("slogcpgrpc cannot encode HTTP request %w", err)
	}
	request := new(logtypepb.HttpRequest)
	if err := protojson.Unmarshal(data, request); err != nil {
		return nil, fmt.Errorf("slogcpgrpc cannot decode HTTP request %w", err)
	}
	u, err := url.Parse(request.RequestUrl)
	if err != nil {
		return nil, fmt.Errorf("slogcpgrpc cannot parse HTTP request URL %w", err)
	}
	r := &http.Request{Method: request.RequestMethod, URL: u, Proto: request.Protocol, Header: make(http.Header)}
	r.Header.Set("User-Agent", request.UserAgent)
	r.Header.Set("Referer", request.Referer)
	return &logging.HTTPRequest{
		Request:                        r,
		RequestSize:                    request.RequestSize,
		Status:                         int(request.Status),
		ResponseSize:                   request.ResponseSize,
		Latency:                        request.Latency.AsDuration(),
		LocalIP:                        request.ServerIp,
		RemoteIP:                       request.RemoteIp,
		CacheHit:                       request.CacheHit,
		CacheValidatedWithOriginServer: request.CacheValidatedWithOriginServer,
		CacheFillBytes:                 request.CacheFillBytes,
		CacheLookup:                    request.CacheLookup,
	}, nil
}

// severity maps a slog level to the nearest Cloud Logging severity at or below
// it. Levels below Debug map to Debug, and LevelDefault or higher maps to
// Default.
func severity(level slog.Level) logging.Severity {
	switch {
	case level >= slogcp.LevelDefault.Level():
		return logging.Default
	case level >= slogcp.LevelEmergency.Level():
		return logging.Emergency
	case level >= slogcp.LevelAlert.Level():
		return logging.Alert
	case level >= slogcp.LevelCritical.Level():
		return logging.Critical
	case level >= slog.LevelError:
		return logging.Error
	case level >= slog.LevelWarn:
		return logging.Warning
	case level >= slogcp.LevelNotice.Level():
		return logging.Notice
	case level >= slog.LevelInfo:
		return logging.Info
	default:
		return logging.Debug
	}
}

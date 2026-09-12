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
	"log/slog"
	"testing"
	"time"

	"cloud.google.com/go/logging"
	"github.com/pjscruggs/slogcp/v2"
)

// BenchmarkBufferedGRPC includes gRPC serialization, local RPC delivery, and
// periodic flushes. Every variant delivers the same structured application data.
func BenchmarkBufferedGRPC(b *testing.B) {
	for _, mode := range []string{"google-client", "exporter", "slogcp-handler"} {
		b.Run(mode, func(b *testing.B) {
			client, _ := newTestClient(b, &recordingServer{discard: true})
			cloudLogger := newTestLogger(client, logging.EntryCountThreshold(100), logging.ConcurrentWriteLimit(4))
			exporter, err := NewExporter(cloudLogger)
			if err != nil {
				b.Fatal(err)
			}
			handler, err := slogcp.NewHandlerWithExporter(exporter, slogcp.WithSourceLocationEnabled(false), slogcp.WithStackTraceEnabled(false))
			if err != nil {
				b.Fatal(err)
			}
			b.Cleanup(func() { _ = handler.Close() })
			ctx := context.Background()
			payload := map[string]any{"message": "request complete", "status": 200, "bytes": 1024}
			entry := slogcp.Entry{Payload: payload, Level: slog.LevelInfo}
			cloudEntry := logging.Entry{Payload: payload, Severity: logging.Info}
			logger := slog.New(handler)
			count := 0
			b.ReportAllocs()
			for b.Loop() {
				switch mode {
				case "google-client":
					cloudLogger.Log(cloudEntry)
				case "exporter":
					entry.Timestamp = time.Now()
					if err := exporter.Export(ctx, entry); err != nil {
						b.Fatal(err)
					}
				case "slogcp-handler":
					logger.Info("request complete", "status", 200, "bytes", 1024)
				}
				count++
				if count%1000 == 0 {
					if err := exporter.Flush(); err != nil {
						b.Fatal(err)
					}
				}
			}
			if err := exporter.Flush(); err != nil {
				b.Fatal(err)
			}
		})
	}
}

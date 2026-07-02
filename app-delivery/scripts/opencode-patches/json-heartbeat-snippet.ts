        // app-delivery-json-heartbeat:start
        const heartbeatIntervalMs = Number.parseInt(process.env.OPENCODE_JSON_HEARTBEAT_INTERVAL_MS ?? "30000", 10)
        const heartbeatEnabled = args.format === "json" && heartbeatIntervalMs > 0 && process.env.OPENCODE_JSON_HEARTBEAT !== "0"
        const heartbeatStartedAt = Date.now()
        let heartbeatLastEventAt = heartbeatStartedAt
        let heartbeatSequence = 0

        function emitHeartbeat(reason: string) {
          if (!heartbeatEnabled) return
          process.stdout.write(
            JSON.stringify({
              type: "heartbeat",
              timestamp: Date.now(),
              sessionID,
              reason,
              sequence: ++heartbeatSequence,
              elapsedMs: Date.now() - heartbeatStartedAt,
              idleMs: Date.now() - heartbeatLastEventAt,
            }) + EOL,
          )
        }
        // app-delivery-json-heartbeat:end

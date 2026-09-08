const { createConfig } = require('./config');
const { detachTransport, startTransport } = require('./transport');
const { createServer } = require('./server');
const { createOutboundProvenanceLedger } = require('./outbound-provenance');
const { drainMediaNotifications } = require('./media');
const { startInboundPendingDrain } = require('./spool');

async function main() {
  const config = createConfig();
  const inboundDrain = startInboundPendingDrain(config, console);
  const outboundProvenance = createOutboundProvenanceLedger(config);
  const { status, initPromise, client } = await startTransport(config, console, { outboundProvenance });
  const server = createServer(config, status, client, { outboundProvenance });
  const mediaRetryTimer = setInterval(() => {
    void drainMediaNotifications(config, console).catch((error) => {
      console.error(JSON.stringify({
        ts: new Date().toISOString(), event: 'media_notification_retry_failed',
        error_class: error?.name || 'Error',
      }));
    });
  }, 2000);
  mediaRetryTimer.unref();

  server.listen(config.httpPort, config.httpHost, () => {
    console.log(
      JSON.stringify({
        ts: new Date().toISOString(),
        event: 'server_listening',
        http_host: config.httpHost,
        http_port: config.httpPort,
        browser_debug_url: config.browserDebugUrl ? '[redacted-local-url]' : null,
        inbound_forward_enabled: config.inboundForwardEnabled,
        external_delivery_enabled: config.externalDeliveryEnabled,
      }),
    );
  });

  const shutdown = async (signal) => {
    console.log(JSON.stringify({ ts: new Date().toISOString(), event: signal }));
    server.close(() => {});
    inboundDrain.stop();
    clearInterval(mediaRetryTimer);
    try {
      if (client) {
        await detachTransport(client, console);
      }
    } catch (error) {
      console.error(JSON.stringify({ ts: new Date().toISOString(), event: 'detach_failed', error: String(error?.message || error) }));
    } finally {
      process.exit(0);
    }
  };

  process.on('SIGTERM', () => { void shutdown('sigterm'); });
  process.on('SIGINT', () => { void shutdown('sigint'); });
  process.on('uncaughtException', (error) => {
    console.error(JSON.stringify({ ts: new Date().toISOString(), event: 'uncaught_exception', error: String(error?.message || error) }));
    process.exitCode = 1;
  });
  process.on('unhandledRejection', (error) => {
    console.error(JSON.stringify({ ts: new Date().toISOString(), event: 'unhandled_rejection', error: String(error?.message || error) }));
    process.exitCode = 1;
  });

  try {
    await initPromise;
  } catch {
    // error already logged; keep the status server alive for observability
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ ts: new Date().toISOString(), event: 'fatal', error: String(error?.message || error) }));
  process.exit(1);
});

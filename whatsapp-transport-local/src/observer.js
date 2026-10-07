const puppeteer = require('puppeteer');

const {
  ObserverService,
  createObserverConfig,
} = require('./observer-core');

async function main() {
  const service = new ObserverService({
    config: createObserverConfig(),
    puppeteer,
    logger: console,
  });

  process.once('SIGTERM', () => { void service.shutdown('SIGTERM'); });
  process.once('SIGINT', () => { void service.shutdown('SIGINT'); });

  await service.start();
}

if (require.main === module) {
  main().catch((error) => {
    console.error(JSON.stringify({
      ts: new Date().toISOString(),
      event: 'observer_fatal',
      error: String(error?.message || error),
    }));
    process.exitCode = 1;
  });
}

module.exports = { main };

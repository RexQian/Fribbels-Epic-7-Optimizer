// Exercise the real Fribbels heroData module with all network entry points denied.
const fs = require('fs');
const path = require('path');
const { fileURLToPath } = require('url');

const candidate = path.resolve(process.argv[2] || '');
const dataRoot = path.resolve(process.argv[3] || path.join(candidate, 'data'));
if (!fs.existsSync(path.join(candidate, 'app/js/lib/heroData.js'))) {
  throw new Error('Candidate path is missing');
}
let networkCalls = 0;
const denied = () => { networkCalls += 1; throw new Error('Network access is denied for offline verification'); };
global.fetch = denied;
global.jQuery = { ajax: denied };
global.Headers = class { constructor() { denied(); } };
global.XMLHttpRequest = class { constructor() { denied(); } };
global.Settings = { getUseLocalCache: () => true };
global.Files = {
  getDataPath: () => dataRoot,
  readFileSync: (filename) => fs.readFileSync(filename, 'utf8'),
  path: (filename) => filename,
};
let artifactCount = 0;
let statsCount = 0;
global.Api = {
  setArtifacts: async (value) => { artifactCount = Object.keys(value).length; },
  setBaseStats: async (value) => { statsCount = Object.keys(value).length; },
};
const originalLog = console.log;
const originalWarn = console.warn;
console.log = () => {};
console.warn = () => {};
async function main() {
  const heroData = require(path.join(candidate, 'app/js/lib/heroData.js'));
  await heroData.initialize();
  const heroes = heroData.getAllHeroData();
  const artifacts = heroData.getAllArtifactData();
  const names = Object.keys(heroes);
  if (!names.length || !Object.keys(artifacts).length || !statsCount || !artifactCount) {
    throw new Error('Offline hero or artifact cache did not load');
  }
  let checkedImages = 0;
  for (const name of names) {
    const hero = heroData.getHeroExtraInfo(name);
    for (const key of ['icon', 'thumbnail']) {
      const url = hero.assets[key];
      if (!url.startsWith('file://')) throw new Error(`Nonlocal ${key}: ${name}`);
      const filename = fileURLToPath(url);
      if (!fs.existsSync(filename) || !fs.statSync(filename).size) {
        throw new Error(`Missing offline ${key}: ${name}`);
      }
      checkedImages += 1;
    }
  }
  if (networkCalls) throw new Error(`Unexpected network calls: ${networkCalls}`);
  originalLog(JSON.stringify({ heroCount: names.length, artifactCount, statsCount, checkedImages, networkCalls }));
}
main().catch((error) => { originalWarn(error); process.exitCode = 1; });

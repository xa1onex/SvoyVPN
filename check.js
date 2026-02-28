const fs = require('fs');
const js = fs.readFileSync('miniapp/need/app_v77.js', 'utf8');
const html = fs.readFileSync('miniapp/index.html', 'utf8');

const regex = /document\.getElementById\(['"]([^'"]+)['"]\)/g;
const missing = new Set();
let match;
while ((match = regex.exec(js)) !== null) {
  const id = match[1];
  if (!html.includes('id="' + id + '"') && !html.includes("id='" + id + "'")) {
    missing.add(id);
  }
}
console.log('Missing from HTML:', Array.from(missing).join(', '));

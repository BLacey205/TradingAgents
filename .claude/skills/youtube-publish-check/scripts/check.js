#!/usr/bin/env node
// Usage: node check.js <metadata.json | ->   (JSON from file or stdin)
// Prints {valid, errors, warnings, value}; exit code 1 when invalid.
const fs = require('fs');
const { validateYouTubeMetadata } = require('./youtube-metadata-validator');

const source = process.argv[2];
if (!source) {
  console.error('Usage: node check.js <metadata.json | ->');
  process.exit(2);
}
let input;
try {
  input = JSON.parse(fs.readFileSync(source === '-' ? 0 : source, 'utf8'));
} catch (error) {
  console.error(`Could not read metadata JSON: ${error.message}`);
  process.exit(2);
}
const result = validateYouTubeMetadata(input);
console.log(JSON.stringify(result, null, 2));
process.exit(result.valid ? 0 : 1);

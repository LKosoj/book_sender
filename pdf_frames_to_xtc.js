#!/usr/bin/env node
// Мост PDF -> XTC: читает кадры RGBA из frames.bin (по одному на страницу PDF),
// кодирует вендоренным энкодером bigbag/epub-to-xtc-converter и собирает
// контейнер XTC (1-bit) / XTCH (2-bit).
// Аргументы: frames.bin count width height fmt title author out.xtc
const fs = require('fs');
const path = require('path');
const encoder = require(path.join(__dirname, 'vendor', 'epub-to-xtc-converter',
                                  'cli', 'encoder.js'));

const [framesPath, countStr, wStr, hStr, fmt, title, author, outPath] =
    process.argv.slice(2);
const count = parseInt(countStr, 10);
const width = parseInt(wStr, 10);
const height = parseInt(hStr, 10);
const isHQ = fmt === 'xtch';
const frameSize = width * height * 4;

const fd = fs.openSync(framesPath, 'r');
const buf = Buffer.alloc(frameSize);
const pages = [];
for (let i = 0; i < count; i++) {
    fs.readSync(fd, buf, 0, frameSize, i * frameSize);
    const rgba = new Uint8ClampedArray(buf);
    pages.push(isHQ ? encoder.encodeXTH(rgba, width, height)
                    : encoder.encodeXTG(rgba, width, height));
}
fs.closeSync(fd);

const container = encoder.buildXTCContainer(
    pages, { title, author }, [], width, height, isHQ);
fs.writeFileSync(outPath, container);
console.log(`OK: ${count} страниц, ${(container.length / 1048576).toFixed(1)} МБ`);

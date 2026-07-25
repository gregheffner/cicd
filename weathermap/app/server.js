// Simple Express proxy server to protect your OWM API key
const express = require('express');
const fetch = require('node-fetch');
const path = require('path');
const https = require('https');
const http = require('http');
const fs = require('fs');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 5000;
const OWM_API_KEY = process.env.OWM_API_KEY;
const OWM_BASE = 'https://api.openweathermap.org/data/2.5';

if (!OWM_API_KEY) {
  console.error('Missing OWM_API_KEY in environment!');
  process.exit(1);
}

// Serve static files from project root (for leaflet-openweathermap.js/css)
app.use('/', express.static(__dirname));
// Serve static frontend
app.use('/', express.static(path.join(__dirname, 'example')));

// Explicitly set Content-Type for index.html
app.get('/', (req, res) => {
  res.type('html'); // sets Content-Type: text/html; charset=utf-8
  res.sendFile(path.join(__dirname, 'example', 'index.html'));
});

// Proxy endpoint for weather by zipcode
app.get('/api/weather', async (req, res) => {
  const { zip, country = 'us', units = 'imperial' } = req.query;
  if (!zip) return res.status(400).json({ error: 'Missing zip parameter' });
  const url = `${OWM_BASE}/weather?zip=${zip},${country}&appid=${OWM_API_KEY}&units=${units}`;
  try {
    const response = await fetch(url);
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    res.status(500).json({ error: 'Failed to fetch weather' });
  }
});

// Proxy for OWM weather icons
app.get('/owm-icon/:icon', async (req, res) => {
  const icon = req.params.icon;
  const url = `https://openweathermap.org/img/w/${icon}`;
  try {
    const response = await fetch(url);
    res.set('Content-Type', response.headers.get('content-type'));
    response.body.pipe(res);
  } catch (err) {
    res.status(500).send('Icon fetch failed');
  }
});

// Proxy for OWM tile layers
app.get('/owm-tile/:layer/:z/:x/:y.png', async (req, res) => {
  const { layer, z, x, y } = req.params;
  const url = `https://tile.openweathermap.org/map/${layer}/${z}/${x}/${y}.png?appid=${OWM_API_KEY}`;
  try {
    const response = await fetch(url);
    res.set('Content-Type', response.headers.get('content-type'));
    response.body.pipe(res);
  } catch (err) {
    res.status(500).send('Tile fetch failed');
  }
});

// You can add more proxy endpoints as needed

const options = {
  key: fs.readFileSync('server.key'),
  cert: fs.readFileSync('server.cert')
};

// Start HTTP server for Kubernetes/internal use
http.createServer(app).listen(8080, () => {
  console.log('HTTP server running on http://localhost:8080');
});

// Start HTTPS server for local/dev use
https.createServer(options, app).listen(PORT, () => {
  console.log(`HTTPS server running on https://localhost:${PORT}`);
});

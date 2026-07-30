const fs = require('fs');
const XLSX = require('xlsx');
const https = require('https');

// Configuration
const CONFIG = {
    ledgerPath: '/mnt/c/Users/digit/Dropbox/Projects/transaction_tracker/ECO Systems General Ledger.csv',
    excelPath: '/mnt/c/Users/digit/Dropbox/Real Estate/Lofty PM/Yhome Transition Reconciliation.xlsx',
    targetColumns: ['Lofty Operating Cash', 'ECO Operating Cash'],
    ecoColumn: 'ECO Operating Cash',
    loftyColumn: 'Lofty Operating Cash',
    marketplaceApi: 'https://api.lofty.ai/prod/properties/v2/marketplace',
    matchColumn: 'Property',
    statePath: '/home/digit/.openclaw/workspace/scripts/ledger_sync_state.json',
    outputPath: '/home/digit/.openclaw/workspace/reports/ledger_sync_preview.json'
};

/**
 * Robust CSV Line Parser
 */
function parseCsvLine(line) {
    const result = [];
    let cur = '';
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
        const char = line[i];
        if (char === '"') {
            if (inQuotes && line[i+1] === '"') {
                cur += '"';
                i++;
            } else {
                inQuotes = !inQuotes;
            }
        } else if (char === ',' && !inQuotes) {
            result.push(cur);
            cur = '';
        } else {
            cur += char;
        }
    }
    result.push(cur);
    return result;
}

/**
 * Normalize address for matching
 */
function normalizeAddress(addr) {
    if (!addr) return '';
    return addr.toString().toLowerCase()
        .replace(/[\.,]/g, '')
        .replace(/\s+/g, ' ')
        .replace(/ street$/i, ' st')
        .replace(/ road$/i, ' rd')
        .replace(/ avenue$/i, ' ave')
        .replace(/ lane$/i, ' ln')
        .replace(/ drive$/i, ' dr')
        .trim();
}

async function syncLedgerToExcel() {
    console.log('Starting sync...');

    // 1. Calculate Balances from Ledger
    const fileContent = fs.readFileSync(CONFIG.ledgerPath, 'utf8').replace(/^\uFEFF/, '');
    const lines = fileContent.split(/\r?\n/);
    const ledgerBalances = new Map();

    for (let i = 1; i < lines.length; i++) {
        if (!lines[i].trim()) continue;
        const parts = parseCsvLine(lines[i]);
        if (parts.length < 9) continue;

        const amount = parseFloat(parts[4]);
        const property = parts[8].trim();

        if (property && !isNaN(amount)) {
            const norm = normalizeAddress(property);
            ledgerBalances.set(norm, (ledgerBalances.get(norm) || 0) + amount);
        }
    }

    // 2. Load Marketplace Data for Reserves
    console.log('Fetching Lofty marketplace data...');
    const marketplaceData = await new Promise((resolve, reject) => {
        https.get(CONFIG.marketplaceApi, (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                try {
                    resolve(JSON.parse(body));
                } catch (e) {
                    reject(e);
                }
            });
            res.on('error', reject);
        });
    });

    const loftyReserves = new Map();
    if (marketplaceData.success && marketplaceData.data.properties) {
        marketplaceData.data.properties.forEach(p => {
            const norm = normalizeAddress(p.address);
            loftyReserves.set(norm, p.curr_maintenance_reserve || 0);
        });
    }

    // 3. Load Excel
    const workbook = XLSX.readFile(CONFIG.excelPath);
    let updatedCount = 0;

    // 4. Process Sheets
    workbook.SheetNames.forEach(sheetName => {
        const sheet = workbook.Sheets[sheetName];
        const range = XLSX.utils.decode_range(sheet['!ref']);

        let ecoColIdx = -1;
        let loftyColIdx = -1;
        let propertyColIdx = -1;

        // Find column indices
        for (let C = range.s.c; C <= range.e.c; ++C) {
            const cell = sheet[XLSX.utils.encode_cell({r: range.s.r, c: C})];
            if (!cell) continue;
            if (cell.v === CONFIG.ecoColumn) ecoColIdx = C;
            if (cell.v === CONFIG.loftyColumn) loftyColIdx = C;
            if (cell.v === CONFIG.matchColumn) propertyColIdx = C;
        }

        if (propertyColIdx === -1) {
            console.log(`Skipping sheet "${sheetName}": Property column not found.`);
            return;
        }

        // Iterate rows
        for (let R = range.s.r + 1; R <= range.e.r; ++R) {
            const propCell = sheet[XLSX.utils.encode_cell({r: R, c: propertyColIdx})];
            if (!propCell || !propCell.v) continue;

            const normProp = normalizeAddress(propCell.v);

            // A. Update ECO Operating Cash
            if (ecoColIdx !== -1 && ledgerBalances.has(normProp)) {
                updatedCount++;
            }

            // B. Update Lofty Operating Cash from Lofty curr_maintenance_reserve.
            if (loftyColIdx !== -1 && loftyReserves.has(normProp)) {
                updatedCount++;
            }
        }
    });

    // 5. Save preview report (Excel writes disabled by policy)
    fs.mkdirSync(require('path').dirname(CONFIG.outputPath), { recursive: true });
    fs.writeFileSync(CONFIG.outputPath, JSON.stringify({
        generatedAt: new Date().toISOString(),
        ledgerPath: CONFIG.ledgerPath,
        excelPath: CONFIG.excelPath,
        targetColumnPolicy: 'Yhome updates are limited to Lofty Operating Cash and ECO Net DAO Funds.',
        targetColumns: CONFIG.targetColumns,
        wouldUpdateCount: updatedCount,
        canonicalUpdater: 'scripts/yhome_operating_cash_apply_verify.py',
        note: 'Legacy workbook preview only. Google Sheet writes must use the canonical gated updater.'
    }, null, 2));

    console.log(`Sync preview complete. Legacy workbook write disabled by policy; report: ${CONFIG.outputPath}`);

    // 6. Update State
    const stats = fs.statSync(CONFIG.ledgerPath);
    fs.writeFileSync(CONFIG.statePath, JSON.stringify({
        lastMtime: stats.mtimeMs,
        lastRun: new Date().toISOString(),
        workbookWriteEnabled: false
    }));
}

// Check for updates or force run
if (process.argv.includes('--force')) {
    syncLedgerToExcel();
} else {
    const stats = fs.statSync(CONFIG.ledgerPath);
    let state = { lastMtime: 0 };
    if (fs.existsSync(CONFIG.statePath)) {
        state = JSON.parse(fs.readFileSync(CONFIG.statePath, 'utf8'));
    }

    if (stats.mtimeMs > state.lastMtime) {
        syncLedgerToExcel();
    } else {
        console.log('No changes detected in ledger since last sync.');
    }
}

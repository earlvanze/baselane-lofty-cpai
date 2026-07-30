const fs = require('fs');
const path = require('path');

const inputFile = 'ECO_Systems_General_Ledger.csv';
const today = new Date().toISOString().split('T')[0];

function splitLedger() {
    const data = fs.readFileSync(inputFile, 'utf8');
    const lines = data.split(/\r?\n/);
    if (lines.length === 0) return;

    const header = lines[0];
    const propertyMap = {};

    for (let i = 1; i < lines.length; i++) {
        const line = lines[i];
        if (!line.trim()) continue;

        // Simple CSV split (comma delimited)
        // Date,Name,Notes,Details,Category,Sub-Category,Amount,Portfolio,Property,Unit,Data Source,Account,Owner,Attachments
        // Property is the 9th column (index 8)
        const parts = line.split(',');
        const property = parts[8] ? parts[8].trim() : 'Unknown';

        if (!propertyMap[property]) {
            propertyMap[property] = [header];
        }
        propertyMap[property].push(line);
    }

    for (const property in propertyMap) {
        if (property === 'Unknown' || property === 'Property') continue;

        const folder = path.join(property, '07 - P&L & Owner Statements');
        if (!fs.existsSync(folder)) {
            fs.mkdirSync(folder, { recursive: true });
        }

        const fileName = `ECO Systems General Ledger ${today} - ${property}.csv`;
        const filePath = path.join(folder, fileName);

        fs.writeFileSync(filePath, propertyMap[property].join('\n'), 'utf8');
        console.log(`Saved: ${filePath}`);
    }
}

splitLedger();

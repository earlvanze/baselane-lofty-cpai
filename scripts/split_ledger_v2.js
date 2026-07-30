const fs = require('fs');
const path = require('path');
const { parse } = require('csv-parse/sync');

const inputFile = '/mnt/c/Users/digit/Dropbox/Projects/transaction_tracker/ECO Systems General Ledger.csv';
const today = new Date().toISOString().split('T')[0];

function splitLedger() {
    console.log(`Reading: ${inputFile}`);
    let content = fs.readFileSync(inputFile, 'utf8');

    // Handle BOM (Byte Order Mark)
    if (content.charCodeAt(0) === 0xFEFF) {
        content = content.slice(1);
    }

    // Parse CSV with header support
    const records = parse(content, {
        columns: true,
        skip_empty_lines: true,
        trim: true,
        relax_quotes: true,
        relax_column_count: true,
        quote: null
    });

    if (records.length === 0) {
        console.log("No records found.");
        return;
    }

    // Get column names for rebuilding header
    const headers = Object.keys(records[0]).map(h => h.replace(/^"|"$/g, ''));
    const propertyMap = {};

    records.forEach(record => {
        // Find property value even if key is quoted
        let propertyKey = Object.keys(record).find(k => k.replace(/^"|"$/g, '') === 'Property');
        let property = record[propertyKey] || 'Unknown';

        // Clean up quoted property names if they exist
        property = property.replace(/^"|"$/g, '').trim();

        if (!propertyMap[property]) {
            propertyMap[property] = [];
        }
        propertyMap[property].push(record);
    });

    console.log(`Found ${Object.keys(propertyMap).length} properties.`);

    for (const property in propertyMap) {
        if (property === 'Unknown' || property === '' || property === 'Property') continue;

        // Define common categories to skip (this is a heuristic)
        const categoriesToSkip = [
            'Operating Expenses', 'Income', 'Utilities', 'Insurance', 'Taxes',
            'Management Fees', 'Repairs & Maintenance', 'Cleaning & Maintenance',
            'Gas & Electric', 'Water & Sewer', 'Garbage & Recycling',
            'General Operating Expenses', 'Loan Payments & Capex', 'General Transfers & Other',
            'Revenue', 'Rents', 'Other Operating Expenses'
        ];

        if (categoriesToSkip.includes(property) || !isNaN(property) || property.includes('/') || property.length < 5) {
            console.log(`Skipping: ${property}`);
            continue;
        }

        // Only process if it looks like an address (usually starts with a number)
        if (!/^\d+/.test(property) && !property.includes('Ave') && !property.includes('St') && !property.includes('Rd') && !property.includes('Dr')) {
             console.log(`Skipping non-address: ${property}`);
             continue;
        }
        const folder = path.join(property, '07 - P&L & Owner Statements');
        if (!fs.existsSync(folder)) {
            try {
                fs.mkdirSync(folder, { recursive: true });
            } catch (err) {
                console.error(`Could not create folder: ${folder}`, err.message);
                continue;
            }
        }

        const fileName = `ECO Systems General Ledger ${today} - ${property}.csv`;
        const filePath = path.join(folder, fileName);

        // Rebuild CSV content
        const csvRows = [headers.join(',')];
        propertyMap[property].forEach(record => {
            const row = headers.map(h => {
                let val = record[h] || '';
                // Quote if contains comma or quote
                if (val.includes(',') || val.includes('"')) {
                    val = `"${val.replace(/"/g, '""')}"`;
                }
                return val;
            });
            csvRows.push(row.join(','));
        });

        fs.writeFileSync(filePath, csvRows.join('\n'), 'utf8');
        console.log(`Saved: ${filePath}`);
    }
}

splitLedger();

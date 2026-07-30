const fs = require('fs');
const path = require('path');
const { parse } = require('csv-parse/sync');

const inputFile = '/mnt/c/Users/digit/Dropbox/Projects/transaction_tracker/ECO Systems General Ledger.csv';
const dropboxBase = '/mnt/c/Users/digit/Dropbox/Real Estate';
const today = new Date().toISOString().split('T')[0];

// Map of property nicknames/short names to their full Dropbox folder paths
const propertyPathMap = {
    '90 Madison Ave': 'NY/90 Madison Ave Albany, NY 12202',
    '88 Madison Ave': 'NY/88 Madison Ave Albany, NY 12202',
    '86 Madison Ave': 'NY/86 Madison Ave Albany, NY 12202',
    '84 Madison Ave': 'NY/84 Madison Ave Albany, NY 12202',
    '82 Madison Ave': 'NY/82 Madison Ave Albany, NY 12202',
    '724 3rd Ave': 'NY/724 3rd Ave, Watervliet, NY 12189',
    '804 S Quitman St': 'CO/804 S Quitman St, Denver, CO 80219',
    '27 Pillar Ln': 'NY/27 Pillar Ln, Selkirk, NY 12158',
    '3805 KIPLING  |  Address: WHEAT RIDGE': 'CO/3805 Kipling St Wheat Ridge, CO 80033',
    '3740 SHERIDAN BLVD  |  Address: DENVER': 'CO/3740 Sheridan Blvd Denver, CO 80212',
    '330 S KALAMATH ST  |  Address: DENVER': 'CO/330 S Kalamath St Denver, CO 80223',
    '86-120 FARRINGTON H  |  Address: WAIANAE': 'HI/85-104 Alawa Pl, Waianae, HI 96792',
    '4450 KAPOLEI PKWY  |  Address: KAPOLEI': 'HI/4450 Kapolei Pkwy Kapolei, HI 96707',
    '3600 W 38TH AVE  |  Address: DENVER': 'CO/3600 W 38th Ave Denver, CO 80211',
    '3065 HAMBURG  ST  |  Address: SCHENECTADY': 'NY/3065 Hamburg St Schenectady, NY 12303',
    '5205 WEST ALAMEDA  |  Address: LAKEWOOD': 'CO/5205 W Alameda Ave Lakewood, CO 80226',
    '9945  S  OSWEGO  ST  |  Address: PARKER': 'CO/9945 S Oswego St Parker, CO 80134',
    '290 S PIERCE ST  |  Address: LAKEWOOD': 'CO/290 S Pierce St Lakewood, CO 80226',
    '11150 S TWENTY MILE  |  Address: PARKER': 'CO/11150 S Twenty Mile Rd Parker, CO 80134',
    '755W ALAMEDA AVE  |  Address: LAKEWOOD': 'CO/755 W Alameda Ave Lakewood, CO 80226',
    '495 SHERIDAN AVE  |  Address: LAKEWOOD': 'CO/495 Sheridan Blvd Lakewood, CO 80226',
    '4401 WADSWORTH BLVD  |  Address: WHEAT RIDGE': 'CO/4401 Wadsworth Blvd Wheat Ridge, CO 80033',
    '300 S FEDERAL BLVD  |  Address: DENVER': 'CO/300 S Federal Blvd Denver, CO 80219'
    // Add more mappings as discovered or needed
};

function resolveStatementsPath(fullPropertyPath) {
    const publicStatementsPath = path.join(fullPropertyPath, 'Public', '07 - P&L & Owner Statements');
    const rootStatementsPath = path.join(fullPropertyPath, '07 - P&L & Owner Statements');
    const publicPath = path.join(fullPropertyPath, 'Public');

    if (path.basename(fullPropertyPath).toLowerCase().endsWith(' public')) {
        return rootStatementsPath;
    }
    if (fs.existsSync(publicStatementsPath)) {
        return publicStatementsPath;
    }
    if (fs.existsSync(rootStatementsPath)) {
        return rootStatementsPath;
    }
    if (fs.existsSync(publicPath)) {
        return publicStatementsPath;
    }
    return rootStatementsPath;
}

function splitLedger() {
    console.log(`Reading: ${inputFile}`);
    let content;
    try {
        content = fs.readFileSync(inputFile, 'utf8');
    } catch (err) {
        console.error("Could not read input file:", err.message);
        return;
    }

    if (content.charCodeAt(0) === 0xFEFF) {
        content = content.slice(1);
    }

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

    const headers = Object.keys(records[0]);
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

    console.log(`Found ${Object.keys(propertyMap).length} unique property entries in CSV.`);

    for (const property in propertyMap) {
        if (property === 'Unknown' || property === '' || property === 'Property') continue;

        // Try to find the full path
        let relativePath = propertyPathMap[property];

        // If not in map, try a loose match in the Dropbox directory if it's a known property
        if (!relativePath) {
             // For now, let's just stick to the map or local workspace for unknown ones
             console.log(`Property "${property}" not in path map. Skipping for now to avoid mess.`);
             continue;
        }

        const fullPropertyPath = path.join(dropboxBase, relativePath);
        const statementsPath = resolveStatementsPath(fullPropertyPath);

        if (!fs.existsSync(statementsPath)) {
            console.log(`P&L & Owner Statements folder not found at: ${statementsPath}. Skipping.`);
            continue;
        }

        const fileName = `ECO Systems General Ledger ${today} - ${property}.csv`;
        const filePath = path.join(statementsPath, fileName);

        const csvRows = [headers.join(',')];
        propertyMap[property].forEach(record => {
            const row = headers.map(h => {
                let val = record[h] || '';
                if (val.includes(',') || val.includes('"')) {
                    val = `"${val.replace(/"/g, '""')}"`;
                }
                return val;
            });
            csvRows.push(row.join(','));
        });

        try {
            fs.writeFileSync(filePath, csvRows.join('\n'), 'utf8');
            console.log(`Saved: ${filePath}`);
        } catch (err) {
            console.error(`Failed to save ${filePath}:`, err.message);
        }
    }
}

splitLedger();

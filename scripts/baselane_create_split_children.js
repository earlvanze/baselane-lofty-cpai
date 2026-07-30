#!/usr/bin/env node
/**
 * Create split child transactions for a Baselane mortgage payment.
 * Usage: node baselane_create_split_children.js <parent_id> <splits_json>
 */
const fs = require('fs');
const path = require('path');

const parentId = process.argv[2];
const splitsJson = process.argv[3];

if (!parentId || !splitsJson) {
  console.error('Usage: baselane_create_split_children.js <parent_id> <splits_json_file>');
  process.exit(2);
}

const splits = JSON.parse(fs.readFileSync(splitsJson, 'utf8'));
const cdpScript = path.join(__dirname, 'baselane_graphql_via_cdp.js');

async function createChild(split, index) {
  const mutation = {
    operationName: 'CreateTransaction',
    variables: {
      input: {
        amount: split.amount,
        tagId: split.tagId,
        propertyId: split.propertyId,
        parentId: parentId,
        note: split.note || '',
        date: split.date
      }
    },
    query: `
      mutation CreateTransaction($input: TransactionInput!) {
        createTransaction(input: $input) {
          id
          amount
          tagId
          parentId
        }
      }
    `
  };

  const tempFile = `/tmp/baselane_child_${index}.json`;
  fs.writeFileSync(tempFile, JSON.stringify(mutation));

  const { execSync } = require('child_process');
  const result = execSync(`node ${cdpScript} ${tempFile}`, { encoding: 'utf8' });
  fs.unlinkSync(tempFile);
  return JSON.parse(result);
}

(async () => {
  console.error(`Creating ${splits.length} split children for parent ${parentId}`);
  for (let i = 0; i < splits.length; i++) {
    try {
      const result = await createChild(splits[i], i);
      console.error(`  [${i+1}/${splits.length}] Created child: ${result.data.createTransaction.id} - ${splits[i].amount}`);
    } catch (err) {
      console.error(`  [${i+1}/${splits.length}] FAILED: ${err.message}`);
    }
  }
  console.log(JSON.stringify({ success: true, parentId, childrenCreated: splits.length }));
})();

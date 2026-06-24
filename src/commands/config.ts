import { Command } from "commander";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const GMGN_CONFIG_DIR = path.join(os.homedir(), ".config", "gmgn");
const KEYPAIR_FILE = path.join(GMGN_CONFIG_DIR, "keypair.pem");
const GMGN_API_URL = "https://gmgn.ai/ai/generateapi";

export function registerConfigCommands(program: Command): void {
  program
    .command("config")
    .description("Generate an Ed25519 key pair and output a pre-filled GMGN API Key creation link")
    .action(async () => {
      fs.mkdirSync(GMGN_CONFIG_DIR, { recursive: true });

      let publicPem: string;

      if (fs.existsSync(KEYPAIR_FILE)) {
        // Reuse existing key pair
        const content = fs.readFileSync(KEYPAIR_FILE, "utf-8");
        const match = content.match(/(-----BEGIN PUBLIC KEY-----[\s\S]+?-----END PUBLIC KEY-----)/);
        if (!match) {
          console.error("Error: keypair.pem exists but public key could not be parsed. Delete ~/.config/gmgn/keypair.pem and try again.");
          process.exit(1);
        }
        publicPem = match[1] + "\n";
      } else {
        // Generate new Ed25519 key pair
        const { privateKey, publicKey } = crypto.generateKeyPairSync("ed25519");
        const privatePem = privateKey.export({ type: "pkcs8", format: "pem" }) as string;
        publicPem = publicKey.export({ type: "spki", format: "pem" }) as string;

        const entry = `# Private Key\n${privatePem}\n# Public Key\n${publicPem}\n`;
        fs.writeFileSync(KEYPAIR_FILE, entry, { mode: 0o600 });
      }

      const link = `${GMGN_API_URL}?pbk=${encodeURIComponent(publicPem)}`;

      console.log(link);
    });
}

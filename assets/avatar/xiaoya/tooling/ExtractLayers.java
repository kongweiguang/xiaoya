import java.awt.image.BufferedImage;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import javax.imageio.ImageIO;

/** 仅从原创分层素材表提取像素，连通组件避免跨格的原画被硬切或混入孤立背景噪点。 */
public final class ExtractLayers {
    private static final String[] IDS = {"Head", "Torso", "ArmL", "ArmR", "LegL", "LegR", "LeafL", "LeafR", "EyeWhiteL", "EyeWhiteR", "PupilL", "PupilR", "BrowL", "BrowR", "MouthOuter", "MouthInner"};
    private static final int[][] BOXES = {
        {233, 177, 785, 606}, {430, 746, 402, 318}, {314, 765, 164, 270}, {793, 765, 150, 270},
        {430, 1027, 180, 140}, {643, 1027, 180, 140}, {84, 141, 221, 504}, {956, 141, 221, 504},
        {409, 456, 110, 151}, {738, 456, 110, 151}, {444, 485, 66, 101}, {768, 485, 66, 101},
        {425, 390, 75, 43}, {759, 390, 75, 43}, {576, 583, 100, 60}, {582, 594, 86, 41}
    };

    /** 头部及从属零件统一下移32源像素消除颈部间隙，源像素保持原样，输出保留定位及来源边界证据。 */
    public static void main(String[] args) throws Exception {
        BufferedImage sheet = ImageIO.read(Path.of(args[0]).toFile());
        Path output = Path.of(args[1]);
        Files.createDirectories(output);
        int width = sheet.getWidth();
        int height = sheet.getHeight();
        int[] labels = new int[width * height];
        List<Component> components = components(sheet, labels);
        StringBuilder manifest = new StringBuilder("canvas\t1280\t1280\n");
        StringBuilder details = new StringBuilder("id\tpixels\tsource-x\tsource-y\tsource-width\tsource-height\n");
        for (int index = 0; index < IDS.length; index++) {
            int cellX = index % 4;
            int cellY = index / 4;
            Component best = components.stream().filter(component -> component.centerX() / 320 == cellX && component.centerY() / 320 == cellY).max(Comparator.comparingInt(component -> component.size)).orElseThrow();
            int left = Math.max(0, best.left - 2);
            int top = Math.max(0, best.top - 2);
            int right = Math.min(width - 1, best.right + 2);
            int bottom = Math.min(height - 1, best.bottom + 2);
            BufferedImage layer = new BufferedImage(right - left + 1, bottom - top + 1, BufferedImage.TYPE_INT_ARGB);
            for (int y = top; y <= bottom; y++) {
                for (int x = left; x <= right; x++) {
                    if (labels[y * width + x] == best.label || nearComponent(labels, width, height, x, y, best.label)) {
                        layer.setRGB(x - left, y - top, sheet.getRGB(x, y));
                    }
                }
            }
            ImageIO.write(layer, "png", output.resolve(IDS[index] + ".png").toFile());
            int[] box = BOXES[index];
            manifest.append(IDS[index]).append('\t').append(IDS[index]).append(".png\t").append(box[0]).append('\t').append(box[1]).append('\t').append(box[2]).append('\t').append(box[3]).append('\n');
            details.append(IDS[index]).append('\t').append(best.size).append('\t').append(left).append('\t').append(top).append('\t').append(layer.getWidth()).append('\t').append(layer.getHeight()).append('\n');
            System.out.println(IDS[index] + " component=" + best.size + " bounds=" + left + "," + top + "," + layer.getWidth() + "," + layer.getHeight());
        }
        Files.writeString(output.resolve("layers.tsv"), manifest.toString(), StandardCharsets.UTF_8);
        Files.writeString(output.resolve("extraction.tsv"), details.toString(), StandardCharsets.UTF_8);
    }

    /** 以主体 alpha 识别连通区域，抛弃不属于任何零件的孤立噪点，原像素颜色不重绘。 */
    private static List<Component> components(BufferedImage image, int[] labels) {
        int width = image.getWidth();
        int height = image.getHeight();
        int[] queue = new int[width * height];
        List<Component> result = new ArrayList<>();
        int label = 0;
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                int start = y * width + x;
                if (labels[start] != 0 || (image.getRGB(x, y) >>> 24) < 64) continue;
                Component component = new Component(++label, x, y);
                int head = 0;
                int tail = 1;
                queue[0] = start;
                labels[start] = label;
                while (head < tail) {
                    int pixel = queue[head++];
                    int px = pixel % width;
                    int py = pixel / width;
                    component.add(px, py);
                    for (int dy = -1; dy <= 1; dy++) {
                        for (int dx = -1; dx <= 1; dx++) {
                            int nx = px + dx;
                            int ny = py + dy;
                            if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
                            int next = ny * width + nx;
                            if (labels[next] == 0 && (image.getRGB(nx, ny) >>> 24) >= 64) {
                                labels[next] = label;
                                queue[tail++] = next;
                            }
                        }
                    }
                }
                if (component.size >= 500) result.add(component);
            }
        }
        return result;
    }

    /** 主体边缘附近保留低 alpha 抗锯齿，避免裁剪让原画轮廓变硬。 */
    private static boolean nearComponent(int[] labels, int width, int height, int x, int y, int label) {
        for (int dy = -2; dy <= 2; dy++) {
            for (int dx = -2; dx <= 2; dx++) {
                int nx = x + dx;
                int ny = y + dy;
                if (nx >= 0 && nx < width && ny >= 0 && ny < height && labels[ny * width + nx] == label) return true;
            }
        }
        return false;
    }

    /** 边界由主体实际像素确定，以应对图片生成时跨越预设格线的情况。 */
    private static final class Component {
        final int label;
        int size;
        int left;
        int right;
        int top;
        int bottom;
        /** 以首像素创建组件，不假设预设格边界等于原画边界。 */
        Component(int label, int x, int y) { this.label = label; left = right = x; top = bottom = y; }
        /** 每个实际像素只计入一个组件。 */
        void add(int x, int y) { size++; left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y); }
        /** 用主体中心决定槽位，跨格边缘仍归属于同一部件。 */
        int centerX() { return (left + right) / 2; }
        /** 用主体中心决定槽位，垂直越界不导致切掉人物头部。 */
        int centerY() { return (top + bottom) / 2; }
    }
}



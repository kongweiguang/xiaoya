import java.awt.AlphaComposite;
import java.awt.Graphics2D;
import java.awt.geom.AffineTransform;
import java.awt.geom.Path2D;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.BufferedOutputStream;
import java.io.DataOutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.imageio.ImageIO;

/** 使用独立开源格式导出器保留原创图层与绑定，工具不依赖 Editor 激活或修改官方运行时。 */
public final class ExportXiaoya {
    private static final String MODEL = "org.umamo.runtime.model.";
    private static final List<?> EMPTY = List.of();
    private static Object channels;
    private static Object white;
    private static Object black;

    /** 将工具固定在调用者指定的实验输出目录，避免写入系统应用或覆盖既有模型。 */
    public static void main(String[] args) throws Exception {
        channels = make(MODEL + "ChannelGrids", Map.of());
        white = make(MODEL + "ColorRgb", 1f, 1f, 1f);
        black = make(MODEL + "ColorRgb", 0f, 0f, 0f);
        Path output = Path.of(args[0]).toAbsolutePath();
        Files.createDirectories(output);
        if (args.length == 1) {
            smoke(output);
        } else {
            exportLayers(Path.of(args[1]).toAbsolutePath(), output);
        }
    }

    /** 先生成原创测试几何，真实 Core 验证通过才继续处理正式图层。 */
    private static void smoke(Path output) throws Exception {
        BufferedImage image = new BufferedImage(32, 32, BufferedImage.TYPE_INT_ARGB);
        Graphics2D graphics = image.createGraphics();
        graphics.setColor(new java.awt.Color(161, 214, 178));
        graphics.fillOval(1, 1, 30, 30);
        graphics.dispose();
        Path source = output.resolve("smoke.png");
        ImageIO.write(image, "png", source.toFile());
        Files.writeString(output.resolve("layers.tsv"), "canvas\t128\t128\nMouthInner\tsmoke.png\t48\t48\t32\t32\n", StandardCharsets.UTF_8);
        exportLayers(output.resolve("layers.tsv"), output);
    }

    /** 源图层定位与参数网格同时进入PSD和CMO；静态后备由相同休止几何渲染，避免嘴线仅在动画正确。 */
    private static void exportLayers(Path manifest, Path output) throws Exception {
        List<String> lines = Files.readAllLines(manifest, StandardCharsets.UTF_8);
        String[] canvas = lines.get(0).split("\t");
        int width = Integer.parseInt(canvas[1]);
        int height = Integer.parseInt(canvas[2]);
        List<Layer> layers = new ArrayList<>();
        for (String line : lines.subList(1, lines.size())) {
            if (line.isBlank() || line.startsWith("#")) continue;
            String[] columns = line.split("\t");
            BufferedImage original = ImageIO.read(manifest.getParent().resolve(columns[1]).toFile());
            if (original == null) throw new IllegalArgumentException("Unreadable PNG: " + columns[1]);
            int targetWidth = Integer.parseInt(columns[4]);
            int targetHeight = Integer.parseInt(columns[5]);
            BufferedImage image = new BufferedImage(targetWidth, targetHeight, BufferedImage.TYPE_INT_ARGB);
            Graphics2D graphics = image.createGraphics();
            graphics.setComposite(AlphaComposite.Src);
            graphics.setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION, java.awt.RenderingHints.VALUE_INTERPOLATION_BICUBIC);
            graphics.drawImage(original, 0, 0, targetWidth, targetHeight, null);
            graphics.dispose();
            layers.add(new Layer(columns[0], image, Integer.parseInt(columns[2]), Integer.parseInt(columns[3])));
        }
        int pageSize = 2048;
        BufferedImage atlas = new BufferedImage(pageSize, pageSize, BufferedImage.TYPE_INT_ARGB);
        Graphics2D packing = atlas.createGraphics();
        packing.setComposite(AlphaComposite.Src);
        int cursorX = 4;
        int cursorY = 4;
        int rowHeight = 0;
        List<Object> drawables = new ArrayList<>();
        List<Object> roots = new ArrayList<>();
        List<Object> renderLeaves = new ArrayList<>();
        List<Object> tiles = new ArrayList<>();
        List<Object> inventory = new ArrayList<>();
        Map<String, Object> rasters = new LinkedHashMap<>();
        Map<String, Integer> pageById = new LinkedHashMap<>();
        BufferedImage preview = new BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB);
        Graphics2D composed = preview.createGraphics();
        composed.setComposite(AlphaComposite.SrcOver);
        for (Layer layer : layers) {
            if (cursorX + layer.image.getWidth() + 4 > pageSize) {
                cursorX = 4;
                cursorY += rowHeight + 8;
                rowHeight = 0;
            }
            if (cursorY + layer.image.getHeight() + 4 > pageSize) throw new IllegalArgumentException("Atlas is too small");
            layer.atlasX = cursorX;
            layer.atlasY = cursorY;
            packing.drawImage(layer.image, cursorX, cursorY, null);
            cursorX += layer.image.getWidth() + 8;
            rowHeight = Math.max(rowHeight, layer.image.getHeight());
            composed.drawImage(layer.image, layer.x, layer.y, null);
            float[] positions = positions(layer);
            float[] uvs = new float[positions.length];
            for (int point = 0; point < positions.length; point += 2) {
                uvs[point] = (layer.atlasX + positions[point] - layer.x) / pageSize;
                uvs[point + 1] = (layer.atlasY + positions[point + 1] - layer.y) / pageSize;
            }
            Object mesh = make(MODEL + "DrawableMesh", positions, uvs, triangles(layer));
            Object grid = meshGrid(layer, positions);
            String tileId = "xiaoya/" + layer.id;
            Object drawable = make(MODEL + "Drawable", layer.id, layer.id, parentOf(layer.id), enumeration(MODEL + "BlendMode", "Normal"), EMPTY, mesh, grid, channels, (float)(100 + drawables.size() * 10), 1f, white, black, false, enumeration(MODEL + "AlphaBlendMode", "Over"), false, true, true, null, 0, tileId, EMPTY);
            drawables.add(drawable);
            roots.add(make(MODEL + "OrgChild$Drawable", layer.id));
            renderLeaves.add(make(MODEL + "RenderDrawable", layer.id));
            Object placement = make(MODEL + "AtlasPlacement", 0, (float)layer.atlasX, (float)layer.atlasY, 1f, 1f, 0f);
            Object reference = make(MODEL + "SourceLayerRef", "xiaoya-art", layer.id, true);
            tiles.add(make(MODEL + "AtlasTile", tileId, layer.id, layer.image.getWidth(), layer.image.getHeight(), placement, reference, false, null));
            inventory.add(make(MODEL + "ArtSourceLayer", layer.id, layer.id, "Xiaoya", layer.x, layer.y, layer.image.getWidth(), layer.image.getHeight(), true, true, null, false, false, false));
            rasters.put(tileId, raster(layer.image));
            pageById.put(layer.id, 0);
        }
        packing.dispose();
        composed.dispose();
        Files.createDirectories(output.resolve("textures"));
        ImageIO.write(atlas, "png", output.resolve("textures/texture_00.png").toFile());
        ImageIO.write(preview, "png", output.resolve("preview.png").toFile());
        writePoster(layers, width, height, output.resolve("poster.png"));
        writePosePoster(layers, width, height, output.resolve("mouth-round.png"), 1f, -1f);
        writePosePoster(layers, width, height, output.resolve("mouth-spread.png"), 1f, 1f);
        writePosePoster(layers, width, height, output.resolve("mouth-open.png"), 1f, 0f);
        writePosePoster(layers, width, height, output.resolve("mouth-micro-open.png"), 0.02f, 0f);
        writePosePoster(layers, width, height, output.resolve("mouth-min-open.png"), 0.01f, 0f);
        writePosePoster(layers, width, height, output.resolve("mouth-small-open.png"), 0.05f, 0f);
        writePosePoster(layers, width, height, output.resolve("mouth-quarter-open.png"), 0.25f, 0f);
        writePosePoster(layers, width, height, output.resolve("mouth-medium-open.png"), 0.5f, 0f);
        writePsd(layers, preview, output.resolve("xiaoya-layers.psd"));
        Object composition = make(MODEL + "AtlasComposition", 1, 2);
        Object modelAtlas = make(MODEL + "PuppetAtlas", List.of(make(MODEL + "AtlasPage", pageSize, pageSize)), tiles, true, composition);
        Object source = make(MODEL + "ArtSource", "xiaoya-art", "xiaoya-layers.psd", "xiaoya-layers.psd", "psd", inventory, null, System.currentTimeMillis(), 0, 0);
        // Core 仅为渲染树中恰好出现一次的网格分配 renderOrder；组织树不能代替此正式导出数据。
        Object renderRoot = make(MODEL + "RenderGroup", null, 500, renderLeaves, channels, null);
        Object puppet = make(MODEL + "PuppetModel", parameters(), EMPTY, deformers(), drawables, roots, null, EMPTY, renderRoot, EMPTY, EMPTY, (float)width, (float)height, width / 2f, -height / 2f, width / 2f, enumeration(MODEL + "RuntimeTarget", "Cubism50"), false, modelAtlas, List.of(source));
        Object exportOptions = make("org.umamo.interop.moc3.Moc3ExportOptions", false, false, false, true, true, true, null);
        Object lowered = call(instance("org.umamo.interop.moc3.export.Moc3Export"), "toMocDocument", puppet, enumeration("org.umamo.format.moc3.moc.MocVersion", System.getProperty("xiaoya.mocVersion", "V30")), null, exportOptions);
        Object document = call(lowered, "getDocument");
        // Umamo 的 MOC 编码器已转换左上原图 UV；再次翻转会让官方 Web shader 采样空白区域。
        byte[] moc3 = (byte[]) call(instance("org.umamo.format.moc3.Moc3"), "write", document);
        Files.write(output.resolve("xiaoya.moc3"), moc3);
        System.out.println("MOC3 bytes=" + moc3.length + " report=" + call(lowered, "getReport"));
        Class<?> functionClass = Class.forName("kotlin.jvm.functions.Function1");
        Object tileFunction = Proxy.newProxyInstance(functionClass.getClassLoader(), new Class<?>[]{functionClass}, (proxy, method, arguments) -> {
            if (method.getName().equals("invoke")) return rasters.get(call(arguments[0], "getRaw"));
            return null;
        });
        Object page = make("org.umamo.interop.cmo3.Cmo3Conversion$AtlasPage", png(atlas), pageSize, pageSize, raster(atlas));
        Object cmoResult = call(instance("org.umamo.interop.cmo3.Cmo3Conversion"), "freshCmo3", puppet, List.of(page), pageById, "小芽 Xiaoya", System.currentTimeMillis(), 0, tileFunction, raster(ImageIO.read(output.resolve("poster.png").toFile())));
        byte[] cmo3 = (byte[]) call(instance("org.umamo.format.cmo3.Cmo3"), "write", call(cmoResult, "getModel"));
        Files.write(output.resolve("xiaoya.cmo3"), cmo3);
        System.out.println("CMO3 bytes=" + cmo3.length + " report=" + call(cmoResult, "getReport"));
        Files.writeString(output.resolve("xiaoya.model3.json"), "{\n  \"Version\": 3,\n  \"FileReferences\": {\"Moc\": \"xiaoya.moc3\", \"Textures\": [\"textures/texture_00.png\"]},\n  \"Groups\": [{\"Target\":\"Parameter\",\"Name\":\"EyeBlink\",\"Ids\":[\"ParamEyeLOpen\",\"ParamEyeROpen\"]},{\"Target\":\"Parameter\",\"Name\":\"LipSync\",\"Ids\":[\"ParamMouthOpenY\"]}]\n}\n", StandardCharsets.UTF_8);
    }

    /** 眼眉增加连续列形成真实笑眼与眉弧；保留唇带和耳座网格，不能用缩放整张头像代替绑定。 */
    private static float[] positions(Layer layer) {
        if (layer.id.startsWith("EyeWhite") || layer.id.startsWith("Pupil") || layer.id.startsWith("Brow")) {
            int columns = 9;
            float[] result = new float[columns * 4];
            for (int column = 0; column < columns; column++) {
                float x = layer.x + layer.image.getWidth() * column / (columns - 1f);
                result[column * 4] = x;
                result[column * 4 + 1] = layer.y;
                result[column * 4 + 2] = x;
                result[column * 4 + 3] = layer.y + layer.image.getHeight();
            }
            return result;
        }
        if (layer.id.startsWith("Leaf")) {
            float[] rows = {0f, 0.40f, 0.52f, 0.56f, 1f};
            float[] result = new float[rows.length * 4];
            for (int row = 0; row < rows.length; row++) {
                result[row * 4] = layer.x;
                result[row * 4 + 1] = layer.y + layer.image.getHeight() * rows[row];
                result[row * 4 + 2] = layer.x + layer.image.getWidth();
                result[row * 4 + 3] = result[row * 4 + 1];
            }
            return result;
        }
        if (!layer.id.equals("MouthOuter")) return quad(layer.x, layer.y, layer.image.getWidth(), layer.image.getHeight());
        int columns = 17;
        float[] result = new float[columns * 4 * 2];
        int width = layer.image.getWidth(), height = layer.image.getHeight();
        for (int column = 0; column < columns; column++) {
            float x = column * (width - 1f) / (columns - 1);
            int sampleX = Math.round(x);
            int top = 0, bottom = height - 1;
            while (top < height - 1 && (layer.image.getRGB(sampleX, top) >>> 24) < 128) top++;
            while (bottom > top && (layer.image.getRGB(sampleX, bottom) >>> 24) < 128) bottom--;
            int upperEnd = top, lowerStart = bottom;
            while (upperEnd < bottom && (layer.image.getRGB(sampleX, upperEnd + 1) >>> 24) >= 64) upperEnd++;
            while (lowerStart > top && (layer.image.getRGB(sampleX, lowerStart - 1) >>> 24) >= 64) lowerStart--;
            float innerTop = upperEnd + 0.5f, innerBottom = lowerStart - 0.5f;
            if (innerTop > innerBottom) innerTop = innerBottom = (top + bottom) / 2f;
            float[] rows = {Math.max(0f, top - 0.5f), innerTop, innerBottom, Math.min(height - 1f, bottom + 0.5f)};
            for (int row = 0; row < 4; row++) {
                int offset = (column * 4 + row) * 2;
                result[offset] = layer.x + x;
                result[offset + 1] = layer.y + rows[row];
            }
        }
        return result;
    }

    /** 眼眉共边带状网格支持弯曲；嘴与叶片保留原接合拓扑，所有三角形维持同一绕序。 */
    private static int[] triangles(Layer layer) {
        if (layer.id.startsWith("EyeWhite") || layer.id.startsWith("Pupil") || layer.id.startsWith("Brow")) {
            int[] indices = new int[8 * 6];
            int offset = 0;
            for (int column = 0; column < 8; column++) {
                int a = column * 2, b = (column + 1) * 2;
                for (int index : new int[]{a, b, b + 1, a, b + 1, a + 1}) indices[offset++] = index;
            }
            return indices;
        }
        if (layer.id.startsWith("Leaf")) {
            int[] indices = new int[4 * 6];
            for (int row = 0; row < 4; row++) {
                int a = row * 2;
                int offset = row * 6;
                for (int index : new int[]{a, a + 1, a + 3, a, a + 3, a + 2}) indices[offset++] = index;
            }
            return indices;
        }
        if (!layer.id.equals("MouthOuter")) return new int[]{0, 1, 2, 0, 2, 3};
        int[] indices = new int[16 * 3 * 6];
        int offset = 0;
        for (int column = 0; column < 16; column++) {
            for (int row = 0; row < 3; row++) {
                int a = column * 4 + row, b = (column + 1) * 4 + row;
                for (int index : new int[]{a, b, b + 1, a, b + 1, a + 1}) indices[offset++] = index;
            }
        }
        return indices;
    }

    /** 笑眼独立于开合、眉角独立于抬眉；把所有组合烘入可编辑关键形态，避免运行时伪造未绑定参数。 */
    private static Object meshGrid(Layer layer, float[] base) throws Exception {
        String id = layer.id.toLowerCase();
        List<Axis> definitions = new ArrayList<>();
        boolean facial = id.equals("head") || id.startsWith("leaf") || id.contains("eye") || id.contains("pupil") || id.contains("brow") || id.contains("mouth");
        if (facial) {
            definitions.add(new Axis("ParamAngleX", -30f, 0f, 30f));
            definitions.add(new Axis("ParamAngleY", -30f, 0f, 30f));
        }
        if (id.contains("mouth")) {
            definitions.add(new Axis("ParamMouthOpenY", 0f, 0.5f, 1f));
            definitions.add(new Axis("ParamMouthForm", -1f, 0f, 1f));
        }
        if (id.contains("eyewhite") || id.contains("pupil")) {
            definitions.add(new Axis(id.endsWith("l") ? "ParamEyeLOpen" : "ParamEyeROpen", 0f, 1f));
            definitions.add(new Axis("ParamEyeSmile", 0f, 1f));
        }
        if (id.contains("pupil")) {
            definitions.add(new Axis("ParamEyeBallX", -1f, 0f, 1f));
            definitions.add(new Axis("ParamEyeBallY", -1f, 0f, 1f));
        }
        if (id.contains("brow")) {
            definitions.add(new Axis(id.endsWith("l") ? "ParamBrowLY" : "ParamBrowRY", -1f, 0f, 1f));
            definitions.add(new Axis(id.endsWith("l") ? "ParamBrowLAngle" : "ParamBrowRAngle", -1f, 0f, 1f));
        }
        if (id.equals("torso")) definitions.add(new Axis("ParamBreath", 0f, 1f));
        if (id.startsWith("leaf")) definitions.add(new Axis("ParamLeafSwing", -1f, 0f, 1f));
        List<Object> axes = new ArrayList<>();
        for (Axis axis : definitions) axes.add(make(MODEL + "KeyformAxis", axis.id, axis.values));
        List<Object> cells = new ArrayList<>();
        int total = 1;
        for (Axis axis : definitions) total *= axis.values.length;
        for (int cell = 0; cell < total; cell++) {
            int[] coordinate = new int[definitions.size()];
            Map<String, Float> values = new LinkedHashMap<>();
            int remainder = cell;
            for (int axis = 0; axis < definitions.size(); axis++) {
                Axis definition = definitions.get(axis);
                coordinate[axis] = remainder % definition.values.length;
                remainder /= definition.values.length;
                values.put(definition.id, definition.values[coordinate[axis]]);
            }
            cells.add(make(MODEL + "KeyformCell", coordinate, make(MODEL + "MeshDeltaForm", localDelta(layer, base, values))));
        }
        return make(MODEL + "KeyformGrid", axes, cells);
    }

    /** 手臂扩大到可见但有限的三十度摆幅；肩根仍藏入躯干，且必须继续通过全范围接合门。 */
    private static List<Object> deformers() throws Exception {
        return List.of(
            rotation("BodyRotation", null, "ParamBodyAngleX", 30f, 640f, 990f, 2.2f),
            rotation("HeadRotation", "BodyRotation", "ParamAngleZ", 30f, 0f, -202f, 5f),
            rotation("LeafLRotation", "HeadRotation", "ParamLeafSwing", 1f, -348f, -376f, 0f),
            rotation("LeafRRotation", "HeadRotation", "ParamLeafSwing", 1f, 325f, -376f, 0f),
            rotation("ArmLRotation", "BodyRotation", "ParamArmL", 1f, -151f, -136f, 30f),
            rotation("ArmRRotation", "BodyRotation", "ParamArmR", 1f, 143f, -136f, -30f)
        );
    }

    /** 三个关键点保持可编辑且确定的幅度，运行时短曲线只在已验收的参数范围内采样。 */
    private static Object rotation(String id, String parent, String parameter, float range, float x, float y, float amplitude) throws Exception {
        Object axis = make(MODEL + "KeyformAxis", parameter, new float[]{-range, 0f, range});
        List<Object> cells = new ArrayList<>();
        for (int index = 0; index < 3; index++) {
            cells.add(make(MODEL + "KeyformCell", new int[]{index}, make(MODEL + "RotationPivotForm", x, y, (index - 1) * amplitude, 1f)));
        }
        Object grid = make(MODEL + "KeyformGrid", List.of(axis), cells);
        return make(MODEL + "Deformer$Rotation", id, id, parent, null, 0f, grid, channels, 1f, white, black, false, false, true, true, true, EMPTY);
    }

    /** 父变形器由图层语义确定，所有脸部细节与嘴巴随头部一起转动。 */
    private static String parentOf(String id) {
        if (id.equals("LeafL")) return "LeafLRotation";
        if (id.equals("LeafR")) return "LeafRRotation";
        if (id.equals("ArmL")) return "ArmLRotation";
        if (id.equals("ArmR")) return "ArmRRotation";
        if (id.equals("Torso") || id.startsWith("Leg")) return "BodyRotation";
        return "HeadRotation";
    }

    /** 笑眼用共用眼轴弯曲、眉角保留厚度；躯干颈根和耳座仍锁定，新增表情不能破坏接合。 */
    private static float[] localDelta(Layer layer, float[] base, Map<String, Float> values) {
        String id = layer.id.toLowerCase();
        float cx = layer.x + layer.image.getWidth() / 2f;
        float cy = layer.y + layer.image.getHeight() / 2f;
        float sx = 1;
        float sy = 1;
        float shiftX = 0;
        float shiftY = 0;
        if (id.contains("mouth")) {
            float open = values.getOrDefault("ParamMouthOpenY", 0f);
            float form = values.getOrDefault("ParamMouthForm", 0f);
            sx = form < 0 ? 1 + 0.40f * form : 1 + 0.35f * form;
            sy = open * (1f + (form < 0 ? -0.25f * form : -0.10f * form));
        }
        if (id.contains("eyewhite") || id.contains("pupil")) {
            String eye = id.endsWith("l") ? "ParamEyeLOpen" : "ParamEyeROpen";
            float openness = values.getOrDefault(eye, 1f);
            sy = 0.015f + 0.985f * openness;
            if (id.contains("pupil")) {
                shiftX += values.getOrDefault("ParamEyeBallX", 0f) * 7;
                shiftY -= values.getOrDefault("ParamEyeBallY", 0f) * 6 * openness;
            }
        }
        if (id.contains("brow")) shiftY -= values.getOrDefault(id.endsWith("l") ? "ParamBrowLY" : "ParamBrowRY", 0f) * 8;
        if (id.equals("torso")) {
            float breath = values.getOrDefault("ParamBreath", 0f);
            sx += breath * 0.007f;
            sy += breath * 0.013f;
            cy = layer.y;
        }
        float turnX = values.getOrDefault("ParamAngleX", 0f) / 30;
        float turnY = values.getOrDefault("ParamAngleY", 0f) / 30;
        boolean shell = id.equals("head") || id.startsWith("leaf");
        shiftX += turnX * (shell ? 7 : 18);
        shiftY -= turnY * (shell ? 12 : 22);
        float[] pivot = pivotOf(parentOf(layer.id));
        float[] delta = new float[base.length];
        for (int index = 0; index < base.length; index += 2) {
            float x = cx + (base[index] - cx) * sx + shiftX;
            float y = cy + (base[index + 1] - cy) * sy + shiftY;
            if (id.contains("eyewhite") || id.contains("pupil")) {
                float smile = values.getOrDefault("ParamEyeSmile", 0f);
                float openness = values.getOrDefault(id.endsWith("l") ? "ParamEyeLOpen" : "ParamEyeROpen", 1f);
                float eyeX = id.endsWith("l") ? 464f : 793f;
                float eyeY = 531.5f + shiftY;
                float ratio = Math.max(-1f, Math.min(1f, (base[index] - eyeX) / 55f));
                // 白色眼眶成为弯曲的细弧；瞳孔在满笑时收为零面积，不把压扁的黑眼珠误作笑眼。
                float thickness = id.contains("pupil") ? 0f : 0.065f;
                float smilingY = eyeY - 26f * (1f - ratio * ratio) * openness + (base[index + 1] - cy) * thickness * openness;
                y = y * (1f - smile) + smilingY * smile;
            }
            if (id.contains("brow")) {
                float amount = values.getOrDefault(id.endsWith("l") ? "ParamBrowLAngle" : "ParamBrowRAngle", 0f);
                double angle = Math.toRadians(amount * 18f);
                float dx = base[index] - cx, dy = base[index + 1] - cy;
                float ratio = dx / (layer.image.getWidth() / 2f);
                x = cx + (float)(dx * Math.cos(angle) - dy * Math.sin(angle)) + shiftX;
                y = cy + (float)(dx * Math.sin(angle) + dy * Math.cos(angle)) + shiftY - Math.abs(amount) * 4f * (1f - ratio * ratio);
            }
            if (id.startsWith("leaf") && base[index + 1] < 403f) {
                float anchorX = id.endsWith("l") ? 292f : 965f;
                double angle = Math.toRadians(values.getOrDefault("ParamLeafSwing", 0f) * (id.endsWith("l") ? 3f : -3f));
                float weight = Math.min(1f, (403f - base[index + 1]) / 65f);
                float dx = base[index] - anchorX, dy = base[index + 1] - 403f;
                x += ((float)(dx * Math.cos(angle) - dy * Math.sin(angle)) - dx) * weight;
                y += ((float)(dx * Math.sin(angle) + dy * Math.cos(angle)) - dy) * weight;
            }
            if (id.equals("mouthouter")) {
                float open = values.getOrDefault("ParamMouthOpenY", 0f);
                float form = values.getOrDefault("ParamMouthForm", 0f);
                float ratioX = (base[index] - cx) / (layer.image.getWidth() / 2f);
                float curve = 2f * (1f - ratioX * ratioX);
                int row = (index / 2) % 4;
                float band = row == 0 ? -1.75f : row == 3 ? 1.75f : 0f;
                float closedY = cy + curve + band;
                float openedY = cy + (base[index + 1] - cy) * (1f + (form < 0 ? -0.25f * form : -0.10f * form));
                y = closedY * (1f - open) + openedY * open + shiftY;
            }
            delta[index] = x - pivot[0] - base[index];
            delta[index + 1] = y - pivot[1] - base[index + 1];
        }
        return delta;
    }

    /** 与父变形器的局部枢轴保持同一世界位置，源图、编辑工程和运行网格才能一致。 */
    private static float[] pivotOf(String parent) {
        return switch (parent) {
            case "BodyRotation" -> new float[]{640, 990};
            case "LeafLRotation" -> new float[]{292, 412};
            case "LeafRRotation" -> new float[]{965, 412};
            case "ArmLRotation" -> new float[]{489, 854};
            case "ArmRRotation" -> new float[]{783, 854};
            default -> new float[]{640, 788};
        };
    }

    /** 保留既有十六参数顺序并新增三个真实面部绑定；笑眼默认零，不能误用开眼默认值。 */
    private static List<Object> parameters() throws Exception {
        List<Object> result = new ArrayList<>();
        for (String id : List.of("ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleX", "ParamEyeBallX", "ParamEyeBallY", "ParamBrowLY", "ParamBrowRY", "ParamMouthForm", "ParamLeafSwing", "ParamArmL", "ParamArmR")) {
            float range = id.contains("Angle") ? 30f : 1f;
            result.add(make(MODEL + "Parameter", id, id, -range, range, 0f, enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        }
        for (String id : List.of("ParamMouthOpenY", "ParamEyeLOpen", "ParamEyeROpen", "ParamBreath")) {
            result.add(make(MODEL + "Parameter", id, id, 0f, 1f, id.contains("Eye") ? 1f : 0f, enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        }
        for (String id : List.of("ParamBrowLAngle", "ParamBrowRAngle")) {
            result.add(make(MODEL + "Parameter", id, id, -1f, 1f, 0f, enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        }
        result.add(make(MODEL + "Parameter", "ParamEyeSmile", "ParamEyeSmile", 0f, 1f, 0f, enumeration(MODEL + "ParameterKind", "NORMAL"), false));
        return result;
    }

    /** 图层四角与纹理四角同序，Core 的运行网格由两个三角形组成。 */
    private static float[] quad(float x, float y, float w, float h) {
        return new float[]{x, y, x + w, y, x + w, y + h, x, y + h};
    }

    /** 保留直通 Alpha 的原始图层，编辑源文件不复用预乘纹理造成暗边。 */
    private static Object raster(BufferedImage image) throws Exception {
        byte[] bytes = new byte[image.getWidth() * image.getHeight() * 4];
        int cursor = 0;
        for (int y = 0; y < image.getHeight(); y++) {
            for (int x = 0; x < image.getWidth(); x++) {
                int argb = image.getRGB(x, y);
                bytes[cursor++] = (byte)(argb >> 16);
                bytes[cursor++] = (byte)(argb >> 8);
                bytes[cursor++] = (byte)argb;
                bytes[cursor++] = (byte)(argb >> 24);
            }
        }
        return make("org.umamo.format.raster.RasterImage", image.getWidth(), image.getHeight(), bytes);
    }

    /** 无临时文件 PNG 编码可直接嵌入编辑工程，保证来源图像完整随模型交付。 */
    private static byte[] png(BufferedImage image) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ImageIO.write(image, "png", output);
        return output.toByteArray();
    }

    /** 后备图直接复用相同参数几何，透明腔闭合和内嘴退化不能再靠缩放整张嘴图近似。 */
    private static void writePoster(List<Layer> layers, int width, int height, Path output) throws Exception {
        writePosePoster(layers, width, height, output, 0f, 0f);
    }

    /** 分姿态预览只渲染已有图层和网格，不生成新原画，方便按相同几何核对嘴形。 */
    private static void writePosePoster(List<Layer> layers, int width, int height, Path output, float open, float form) throws Exception {
        BufferedImage poster = new BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB);
        Graphics2D graphics = poster.createGraphics();
        graphics.setComposite(AlphaComposite.SrcOver);
        graphics.setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION, java.awt.RenderingHints.VALUE_INTERPOLATION_BICUBIC);
        for (Layer layer : layers) {
            if (layer.id.startsWith("Mouth")) {
                renderMesh(graphics, layer, Map.of("ParamMouthOpenY", open, "ParamMouthForm", form));
            } else graphics.drawImage(layer.image, layer.x, layer.y, null);
        }
        graphics.dispose();
        ImageIO.write(poster, "png", output.toFile());
    }

    /** Java2D只作绑定网格的离线快照；零面积三角形跳过，避免闭嘴退化造成逆矩阵无穷值。 */
    private static void renderMesh(Graphics2D graphics, Layer layer, Map<String, Float> values) {
        float[] base = positions(layer), delta = localDelta(layer, base, values), pivot = pivotOf(parentOf(layer.id));
        int[] indices = triangles(layer);
        for (int triangle = 0; triangle < indices.length; triangle += 3) {
            double[] sx = new double[3], sy = new double[3], tx = new double[3], ty = new double[3];
            for (int corner = 0; corner < 3; corner++) {
                int vertex = indices[triangle + corner] * 2;
                sx[corner] = base[vertex] - layer.x; sy[corner] = base[vertex + 1] - layer.y;
                tx[corner] = base[vertex] + delta[vertex] + pivot[0];
                ty[corner] = base[vertex + 1] + delta[vertex + 1] + pivot[1];
            }
            double determinant = (sx[1] - sx[0]) * (sy[2] - sy[0]) - (sx[2] - sx[0]) * (sy[1] - sy[0]);
            double targetArea = (tx[1] - tx[0]) * (ty[2] - ty[0]) - (tx[2] - tx[0]) * (ty[1] - ty[0]);
            if (Math.abs(determinant) < 0.000001 || Math.abs(targetArea) < 0.000001) continue;
            double a = ((tx[1] - tx[0]) * (sy[2] - sy[0]) - (tx[2] - tx[0]) * (sy[1] - sy[0])) / determinant;
            double c = (-(tx[1] - tx[0]) * (sx[2] - sx[0]) + (tx[2] - tx[0]) * (sx[1] - sx[0])) / determinant;
            double b = ((ty[1] - ty[0]) * (sy[2] - sy[0]) - (ty[2] - ty[0]) * (sy[1] - sy[0])) / determinant;
            double d = (-(ty[1] - ty[0]) * (sx[2] - sx[0]) + (ty[2] - ty[0]) * (sx[1] - sx[0])) / determinant;
            Path2D clip = new Path2D.Double();
            clip.moveTo(tx[0], ty[0]); clip.lineTo(tx[1], ty[1]); clip.lineTo(tx[2], ty[2]); clip.closePath();
            Graphics2D triangleGraphics = (Graphics2D) graphics.create();
            triangleGraphics.clip(clip);
            triangleGraphics.drawImage(layer.image, new AffineTransform(a, b, c, d, tx[0] - a * sx[0] - c * sy[0], ty[0] - b * sx[0] - d * sy[0]), null);
            triangleGraphics.dispose();
        }
    }

    /** PSD 使用公开格式的无压缩 RGBA 图层，源文件保持真实分层而不是把合成图放进单个图层。 */
    private static void writePsd(List<Layer> layers, BufferedImage merged, Path output) throws Exception {
        ByteArrayOutputStream layerInfoBuffer = new ByteArrayOutputStream();
        DataOutputStream layerInfo = new DataOutputStream(layerInfoBuffer);
        layerInfo.writeShort(-layers.size());
        List<Layer> topFirst = new ArrayList<>(layers);
        Collections.reverse(topFirst);
        for (Layer layer : topFirst) {
            layerInfo.writeInt(layer.y);
            layerInfo.writeInt(layer.x);
            layerInfo.writeInt(layer.y + layer.image.getHeight());
            layerInfo.writeInt(layer.x + layer.image.getWidth());
            layerInfo.writeShort(4);
            for (int channel : new int[]{0, 1, 2, -1}) {
                layerInfo.writeShort(channel);
                layerInfo.writeInt(2 + layer.image.getWidth() * layer.image.getHeight());
            }
            layerInfo.writeBytes("8BIMnorm");
            layerInfo.writeByte(255);
            layerInfo.writeByte(0);
            layerInfo.writeByte(0);
            layerInfo.writeByte(0);
            byte[] name = layer.id.getBytes(StandardCharsets.US_ASCII);
            int paddedNameLength = (name.length + 1 + 3) / 4 * 4;
            layerInfo.writeInt(8 + paddedNameLength);
            layerInfo.writeInt(0);
            layerInfo.writeInt(0);
            layerInfo.writeByte(name.length);
            layerInfo.write(name);
            layerInfo.write(new byte[paddedNameLength - name.length - 1]);
        }
        for (Layer layer : topFirst) {
            for (int channel = 0; channel < 4; channel++) {
                layerInfo.writeShort(0);
                writeChannel(layerInfo, layer.image, channel);
            }
        }
        if (layerInfoBuffer.size() % 2 != 0) layerInfo.writeByte(0);
        ByteArrayOutputStream layerMaskBuffer = new ByteArrayOutputStream();
        DataOutputStream layerMask = new DataOutputStream(layerMaskBuffer);
        layerMask.writeInt(layerInfoBuffer.size());
        layerMask.write(layerInfoBuffer.toByteArray());
        layerMask.writeInt(0);
        try (DataOutputStream psd = new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(output)))) {
            psd.writeBytes("8BPS");
            psd.writeShort(1);
            psd.write(new byte[6]);
            psd.writeShort(4);
            psd.writeInt(merged.getHeight());
            psd.writeInt(merged.getWidth());
            psd.writeShort(8);
            psd.writeShort(3);
            psd.writeInt(0);
            psd.writeInt(0);
            psd.writeInt(layerMaskBuffer.size());
            psd.write(layerMaskBuffer.toByteArray());
            psd.writeShort(0);
            for (int channel = 0; channel < 4; channel++) writeChannel(psd, merged, channel);
        }
    }

    /** PSD 的每个通道独立存储，透明度随原像素保存，方便后续美术工具继续编辑。 */
    private static void writeChannel(DataOutputStream output, BufferedImage image, int channel) throws Exception {
        int shift = new int[]{16, 8, 0, 24}[channel];
        for (int y = 0; y < image.getHeight(); y++) {
            for (int x = 0; x < image.getWidth(); x++) output.writeByte(image.getRGB(x, y) >>> shift);
        }
    }

    /** Kotlin 值类构造器在 JVM 名称上被隐藏；反射只调用开源库原始构造器，不改其安全逻辑。 */
    private static Object make(String className, Object... arguments) throws Exception {
        Class<?> type = Class.forName(className);
        for (Constructor<?> constructor : type.getDeclaredConstructors()) {
            Class<?>[] types = constructor.getParameterTypes();
            if (types.length != arguments.length || !compatible(types, arguments)) continue;
            constructor.setAccessible(true);
            return constructor.newInstance(arguments);
        }
        throw new IllegalArgumentException("No constructor " + className + " with " + arguments.length + " arguments; available=" + Arrays.toString(type.getDeclaredConstructors()));
    }

    /** 公共单例方法按运行参数匹配，隔离独立导出器与项目前端依赖。 */
    private static Object call(Object receiver, String name, Object... arguments) throws Exception {
        for (Method method : receiver.getClass().getMethods()) {
            if (!method.getName().equals(name) || method.getParameterCount() != arguments.length || !compatible(method.getParameterTypes(), arguments)) continue;
            return method.invoke(receiver, arguments);
        }
        throw new IllegalArgumentException("No method " + receiver.getClass().getName() + "." + name);
    }

    /** Kotlin 单例和枚举均通过其发行 JAR 的正式字段取得，避免重建序列化规则。 */
    private static Object instance(String name) throws Exception {
        return Class.forName(name).getField("INSTANCE").get(null);
    }

    /** 使用发行包自身的枚举隔离内部序号变化，MOC 格式目标由导出调用显式指定。 */
    @SuppressWarnings({"rawtypes", "unchecked"})
    private static Object enumeration(String className, String value) throws Exception {
        return Enum.valueOf((Class)Class.forName(className), value);
    }

    /** Java 原始类型与包装类型必须匹配，错误签名显式失败，避免生成部分有效文件。 */
    private static boolean compatible(Class<?>[] types, Object[] values) {
        for (int i = 0; i < types.length; i++) {
            if (values[i] == null) {
                if (types[i].isPrimitive()) return false;
            } else if (types[i].isPrimitive()) {
                if ((types[i] == int.class && !(values[i] instanceof Integer)) || (types[i] == float.class && !(values[i] instanceof Float)) || (types[i] == long.class && !(values[i] instanceof Long)) || (types[i] == boolean.class && !(values[i] instanceof Boolean))) return false;
            } else if (!types[i].isInstance(values[i])) return false;
        }
        return true;
    }

    /** 图层位置属于原创原画数据，图集位置则由导出时计算，不混淆二者坐标。 */
    private static final class Layer {
        final String id;
        final BufferedImage image;
        final int x;
        final int y;
        int atlasX;
        int atlasY;
        /** 固定源数据供连续网格绑定复用。 */
        Layer(String id, BufferedImage image, int x, int y) { this.id = id; this.image = image; this.x = x; this.y = y; }
    }

    /** 参数轴的数据与 JVM 导出器对象分离，便于检查每个嘴型组合是否完整覆盖。 */
    private static final class Axis {
        final String id;
        final float[] values;
        /** 保留顺序和值，不依赖动态运行时推断关键形态。 */
        Axis(String id, float... values) { this.id = id; this.values = values; }
    }
}


import { CubismLogError } from '@framework/utils/cubismdebug';
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncprocessorcri';
import { ICubismMotionSyncProcessor } from './icubismmotionsyncprocessor';
import { MotionSyncAnalysisConfig_CRI } from './motionsyncconfig_cri';
var ToPointer = Live2DCubismMotionSyncCore.ToPointer;
export class CubismMotionSyncProcessorCRI extends ICubismMotionSyncProcessor {
    getSampleRate() {
        return this._sampleRate;
    }
    getBitDepth() {
        return this._bitDepth;
    }
    /** 保留官方分析流程，临时配置在 finally 释放，且原生缓冲按字节分配。 */
    Analyze(samples, beginIndex, blendRatio, smoothing, audioLevelEffectRatio, analysisResult) {
        const samplesSize = samples.getSize();
        if (samplesSize <
            this.getEngine().getEngineHandle().getRequireSampleCount(this.getContextHandle().getContext())) {
            CubismLogError('The argument is invalid. Please check that the samples is the correct value.');
            return null;
        }
        if (!(0 <= beginIndex && beginIndex < samples.getSize())) {
            CubismLogError('The value of beginIndex is incorrect. It should be less than the length of samples.');
            return null;
        }
        if (!(0.0 <= blendRatio && blendRatio <= 1.0)) {
            CubismLogError('The value of blend ratio is incorrect. The value is limited to between 0.0 and 1.0.');
            return null;
        }
        if (!(1 <= smoothing && smoothing <= 100)) {
            CubismLogError('The value of smoothing is incorrect. The value is limited to between 1 and 100.');
            return null;
        }
        if (!(0.0 <= audioLevelEffectRatio && audioLevelEffectRatio <= 1.0)) {
            CubismLogError('The value of audio level effect ratio is incorrect. The value is limited to between 0.0 and 1.0.');
            return null;
        }
        if (!analysisResult) {
            CubismLogError('The result instance is null.');
            return null;
        }
        const analysisConfig = new MotionSyncAnalysisConfig_CRI(blendRatio, smoothing, audioLevelEffectRatio);
        try {
            const analysisConfigBuffer = analysisConfig.toNativeArray(false);
            // ポインタを生成
            if (!this._analysisConfigNativePtr || this._analysisConfigNativePtr == 0) {
                this._analysisConfigNativePtr = ToPointer.Malloc(analysisConfigBuffer.byteLength);
            }
            ToPointer.AddValuePtrFloat(this._analysisConfigNativePtr, 0, analysisConfigBuffer[0]);
            ToPointer.AddValuePtrInt32(this._analysisConfigNativePtr, 4, analysisConfigBuffer[1]);
            ToPointer.AddValuePtrFloat(this._analysisConfigNativePtr, 8, analysisConfigBuffer[2]);
            const analysisResultBuffer = analysisResult.toNativeArray(false);
            // ポインタを生成
            if (!this._analysisResultNativePtr || this._analysisResultNativePtr == 0) {
                this._analysisResultNativePtr = ToPointer.Malloc(analysisResultBuffer.length * analysisResultBuffer.BYTES_PER_ELEMENT);
            }
            ToPointer.AddValuePtrInt32(this._analysisResultNativePtr, 0, analysisResultBuffer[0]);
            ToPointer.AddValuePtrInt32(this._analysisResultNativePtr, 4, analysisResultBuffer[1]);
            ToPointer.AddValuePtrInt32(this._analysisResultNativePtr, 8, analysisResultBuffer[2]);
            const ret = this.getEngine()
                .getEngineHandle()
                .analyze(this.getContextHandle().getContext(), samples._ptr, beginIndex, samplesSize - beginIndex, this._analysisResultNativePtr, this._analysisConfigNativePtr);
            if (!ret) {
                CubismLogError('Failed to analyze.');
                return null;
            }
            // データを引っ張ってくる。
            analysisResult.ConvertNativeAnalysisResult(this._analysisResultNativePtr);
            return analysisResult;
        }
        finally {
            analysisConfig.releaseNativeArray();
        }
    }
    constructor(engine, contextHandle, mappingList, sampleRate, bitDepth) {
        super(engine, contextHandle, mappingList);
        this._analysisConfigNativePtr = 0;
        this._analysisResultNativePtr = 0;
        this._sampleRate = sampleRate;
        this._bitDepth = bitDepth;
    }
    /** 关闭处理器时一并释放分析结构，挂断不能给全局引擎遗留原生缓冲。 */
    Close() {
        if (this.isClosed())
            return;
        this.release();
        super.Close();
    }
    /** 缓冲可被初始化失败路径回收，因此允许指针尚未创建或已经清零。 */
    release() {
        ToPointer.Free(this._analysisConfigNativePtr);
        this._analysisConfigNativePtr = 0;
        ToPointer.Free(this._analysisResultNativePtr);
        this._analysisResultNativePtr = 0;
    }
}
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismMotionSyncProcessorCRI = $.CubismMotionSyncProcessorCRI;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));

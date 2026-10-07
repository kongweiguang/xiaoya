/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmString } from '@framework/type/csmstring';
import { csmVector } from '@framework/type/csmvector';
import { CubismModel } from '@framework/model/cubismmodel';
import { EngineType } from './cubismmotionsyncutil';
import { CubismMotionSyncEngineMappingInfo } from './cubismmotionsyncenginemappinginfo';
export declare class CubismMotionSyncData {
    /**
     * インスタンスの作成
     * @param buffer    physics3.jsonが読み込まれているバッファ
     * @param size      バッファのサイズ
     * @return 作成されたインスタンス
     */
    static create(model: CubismModel, buffer: ArrayBuffer, size: number): CubismMotionSyncData;
    /**
     * インスタンスを破棄する
     * @param motionSyncData 破棄するインスタンス
     */
    static delete(motionSyncData: CubismMotionSyncData): void;
    /**
     * motionsync3.jsonをパースする。
     *
     * @param motionSyncJson  motionsync3.jsonが読み込まれているバッファ
     * @param size        バッファのサイズ
     */
    parse(model: CubismModel, motionSyncJson: ArrayBuffer, size: number): void;
    /**
     * デストラクタ相当の処理
     */
    release(): void;
    getVersion(): number;
    getMeta(): CubismMotionSyncDataMeta;
    getSetting(index: number): CubismMotionSyncDataSetting;
    getSettingCount(): number;
    getMappingInfoList(index: number): csmVector<CubismMotionSyncEngineMappingInfo>;
    /**
     * コンストラクタ
     */
    private constructor();
    private _version;
    private _meta;
    private _settings;
    private _settingCount;
}
/**
 * モーションシンク設定のユースケース
 */
export declare enum CubismMotionSyncDataUseCase {
    UseCase_Mouth = 0,
    UseCase_Unknown = 1
}
/**
 * モーションシンク設定のマッピングタイプ
 */
export declare enum CubismMotionSyncDataMappingType {
    MappingType_Shape = 0,
    MappingType_Unknown = 1
}
/**
 * モーションシンク設定のIdと名称
 */
export declare class CubismMotionSyncDataMetaDictionary {
    id: csmString;
    name: csmString;
}
/**
 * メタデータ
 */
export declare class CubismMotionSyncDataMeta {
    settingCount: number;
    dictionary: csmVector<CubismMotionSyncDataMetaDictionary>;
}
/**
 * CubismParametarsに登録されているCubismParametar
 */
export declare class CubismMotionSyncDataCubismParameter {
    name: csmString;
    id: csmString;
    min: number;
    max: number;
    damper: number;
    smooth: number;
    parameterIndex: number;
}
/**
 * AudioParametersに登録されている音声の要素
 */
export declare class CubismMotionSyncDataAudioParameter {
    name: csmString;
    id: csmString;
    min: number;
    max: number;
    scale: number;
    enabled: boolean;
}
/**
 * マッピングのターゲット
 */
export declare class CubismMotionSyncDataMappingTarget {
    id: csmString;
    value: number;
}
/**
 * マッピングデータ
 */
export declare class CubismMotionSyncDataMapping {
    type: CubismMotionSyncDataMappingType;
    audioId: csmString;
    targetList: csmVector<CubismMotionSyncDataMappingTarget>;
}
export declare class CubismMotionSyncDataSetting {
    id: csmString;
    analysisType: EngineType;
    useCase: CubismMotionSyncDataUseCase;
    cubismParameterList: csmVector<CubismMotionSyncDataCubismParameter>;
    audioParameterList: csmVector<CubismMotionSyncDataAudioParameter>;
    mappingList: csmVector<CubismMotionSyncDataMapping>;
    blendRatio: number;
    smoothing: number;
    sampleRate: number;
}
import * as $ from './cubismmotionsyncdata';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncData: typeof $.CubismMotionSyncData;
    type CubismMotionSyncData = $.CubismMotionSyncData;
}

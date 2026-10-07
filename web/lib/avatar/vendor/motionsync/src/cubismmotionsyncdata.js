/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { CubismLogWarning } from '@framework/utils/cubismdebug';
import { CubismMotionSyncDataJson } from './cubismmotionsyncdatajson';
import { CubismMotionSyncEngineMappingInfo } from './cubismmotionsyncenginemappinginfo';
export class CubismMotionSyncData {
    /**
     * インスタンスの作成
     * @param buffer    physics3.jsonが読み込まれているバッファ
     * @param size      バッファのサイズ
     * @return 作成されたインスタンス
     */
    static create(model, buffer, size) {
        const ret = new CubismMotionSyncData();
        ret.parse(model, buffer, size);
        return ret;
    }
    /**
     * インスタンスを破棄する
     * @param motionSyncData 破棄するインスタンス
     */
    static delete(motionSyncData) {
        if (motionSyncData != null) {
            motionSyncData.release();
            motionSyncData = null;
        }
    }
    /**
     * motionsync3.jsonをパースする。
     *
     * @param motionSyncJson  motionsync3.jsonが読み込まれているバッファ
     * @param size        バッファのサイズ
     */
    parse(model, motionSyncJson, size) {
        let json = new CubismMotionSyncDataJson(motionSyncJson, size);
        if (json._json == null || model == null) {
            CubismLogWarning('Failed to parse .motionsync3.json.');
            return;
        }
        this._version = json.getVersion();
        this._meta = json.getMeta();
        this._settings = new csmVector();
        for (let index = 0; index < this._meta.settingCount; index++) {
            this._settings.pushBack(json.getSetting(index));
        }
        this._settingCount = this._settings.getSize();
        for (let index = 0; index < this._settings.getSize(); index++) {
            const cubismParameterList = this._settings.at(index).cubismParameterList;
            const parameterCount = cubismParameterList.getSize();
            for (let cubismParameterIndex = 0; cubismParameterIndex < parameterCount; cubismParameterIndex++) {
                let parameterIndex = -1;
                for (let modelParameterIndex = 0; modelParameterIndex < model.getParameterCount(); modelParameterIndex++) {
                    if (model
                        .getParameterId(modelParameterIndex)
                        .isEqual(cubismParameterList.at(cubismParameterIndex).id)) {
                        parameterIndex = modelParameterIndex;
                        break;
                    }
                }
                cubismParameterList.at(cubismParameterIndex).parameterIndex =
                    parameterIndex;
                if (parameterIndex < 0) {
                    CubismLogWarning(`Failed to find parameter index for ${cubismParameterList.at(cubismParameterIndex).id.s}`);
                }
            }
        }
        json.release();
        json = void 0;
        json = null;
    }
    /**
     * デストラクタ相当の処理
     */
    release() {
        this._version = void 0;
        this._meta = void 0;
        this._meta = null;
        this._settings = void 0;
        this._settings = null;
        this._settingCount = 0;
    }
    getVersion() {
        return this._version;
    }
    getMeta() {
        return this._meta;
    }
    getSetting(index) {
        return this._settings.at(index);
    }
    getSettingCount() {
        return this._settingCount;
    }
    getMappingInfoList(index) {
        if (this._settings.getSize() <= index) {
            return null;
        }
        const mappingInfoList = new csmVector();
        const setting = this.getSetting(index);
        for (let audioParamIndex = 0; audioParamIndex < setting.audioParameterList.getSize(); audioParamIndex++) {
            const audioParamId = setting.audioParameterList.at(audioParamIndex).id;
            const modelParamIds = new csmVector();
            const modelParamValues = new csmVector();
            for (let serchIndex = 0; serchIndex < setting.audioParameterList.getSize(); serchIndex++) {
                if (audioParamId.isEqual(setting.mappingList.at(serchIndex).audioId.s)) {
                    for (let cubismPramIndex = 0; cubismPramIndex < setting.cubismParameterList.getSize(); cubismPramIndex++) {
                        const target = setting.mappingList.at(serchIndex).targetList.at(cubismPramIndex);
                        modelParamIds.pushBack(target.id);
                        modelParamValues.pushBack(target.value);
                    }
                    break;
                }
            }
            const scale = setting.audioParameterList.at(audioParamIndex).scale;
            const enabled = setting.audioParameterList.at(audioParamIndex).enabled;
            mappingInfoList.pushBack(new CubismMotionSyncEngineMappingInfo(audioParamId, modelParamIds, modelParamValues, scale, enabled));
        }
        return mappingInfoList;
    }
    /**
     * コンストラクタ
     */
    constructor() {
        this._version = 0;
        this._meta = null;
        this._settings = null;
    }
}
/**
 * モーションシンク設定のユースケース
 */
export var CubismMotionSyncDataUseCase;
(function (CubismMotionSyncDataUseCase) {
    CubismMotionSyncDataUseCase[CubismMotionSyncDataUseCase["UseCase_Mouth"] = 0] = "UseCase_Mouth";
    CubismMotionSyncDataUseCase[CubismMotionSyncDataUseCase["UseCase_Unknown"] = 1] = "UseCase_Unknown";
})(CubismMotionSyncDataUseCase || (CubismMotionSyncDataUseCase = {}));
/**
 * モーションシンク設定のマッピングタイプ
 */
export var CubismMotionSyncDataMappingType;
(function (CubismMotionSyncDataMappingType) {
    CubismMotionSyncDataMappingType[CubismMotionSyncDataMappingType["MappingType_Shape"] = 0] = "MappingType_Shape";
    CubismMotionSyncDataMappingType[CubismMotionSyncDataMappingType["MappingType_Unknown"] = 1] = "MappingType_Unknown";
})(CubismMotionSyncDataMappingType || (CubismMotionSyncDataMappingType = {}));
/**
 * モーションシンク設定のIdと名称
 */
export class CubismMotionSyncDataMetaDictionary {
}
/**
 * メタデータ
 */
export class CubismMotionSyncDataMeta {
}
/**
 * CubismParametarsに登録されているCubismParametar
 */
export class CubismMotionSyncDataCubismParameter {
}
/**
 * AudioParametersに登録されている音声の要素
 */
export class CubismMotionSyncDataAudioParameter {
}
/**
 * マッピングのターゲット
 */
export class CubismMotionSyncDataMappingTarget {
}
/**
 * マッピングデータ
 */
export class CubismMotionSyncDataMapping {
}
export class CubismMotionSyncDataSetting {
}
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncdata';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismMotionSyncData = $.CubismMotionSyncData;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
